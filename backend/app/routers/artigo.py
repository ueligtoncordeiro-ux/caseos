import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.models.schemas import CKO, IniciarResponse, StatusResponse
from app.models.database import Sessao, get_db, Usuario
from app.agents.orchestrator import executar_pipeline, executar_pipeline_demo
from app.services.auth import get_verified_user, check_quota

router = APIRouter(prefix="/artigo", tags=["artigo"])


@router.post("/demo", response_model=IniciarResponse)
async def iniciar_demo(
    cko: CKO,
    background_tasks: BackgroundTasks,
):
    """Pipeline de demonstração — sem autenticação. Gera apenas Introdução + Caso Clínico."""
    background_tasks.add_task(executar_pipeline_demo, cko.sessao_id, cko)

    return IniciarResponse(
        sessao_id=cko.sessao_id,
        status="iniciado",
        mensagem="Pipeline de demonstração iniciado. Acompanhe via WebSocket.",
    )


@router.post("/iniciar", response_model=IniciarResponse)
async def iniciar_geracao(
    cko: CKO,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(check_quota),
):
    result = await db.execute(
        select(Sessao).where(Sessao.external_id == cko.sessao_id)
    )
    existing = result.scalar_one_or_none()

    if existing:
        if existing.user_id and existing.user_id != user.id:
            raise HTTPException(status_code=403, detail="Sessão pertence a outro usuário.")
        if existing.status in ("gerando", "redigindo", "revisando", "finalizando"):
            raise HTTPException(status_code=409, detail="Sessão já está sendo processada.")
        if existing.status == "concluido":
            raise HTTPException(status_code=409,
                                detail="Sessão já concluída. Inicie um novo relato.")
        existing.status  = "rascunho"
        existing.cko     = cko.model_dump()
        existing.user_id = user.id
        await db.commit()
    else:
        # Título automático a partir da queixa principal
        queixa = cko.historia.queixa_principal or ""
        titulo_auto = queixa[:80] if queixa else f"Relato {cko.sessao_id[:8]}"
        sessao = Sessao(
            external_id=cko.sessao_id,
            status="rascunho",
            cko=cko.model_dump(),
            user_id=user.id,
            titulo=titulo_auto,
        )
        db.add(sessao)
        await db.commit()

    background_tasks.add_task(executar_pipeline, cko.sessao_id, cko)

    return IniciarResponse(
        sessao_id=cko.sessao_id,
        status="iniciado",
        mensagem="Pipeline iniciado. Acompanhe via WebSocket.",
    )


@router.get("/historico")
async def historico(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=10, ge=1, le=50),
    status: Optional[str] = Query(default=None),
):
    """Lista os relatos do usuário com paginação."""
    q = select(Sessao).where(Sessao.user_id == user.id)
    if status:
        q = q.where(Sessao.status == status)
    q = q.order_by(desc(Sessao.created_at))

    total_q = select(func.count()).select_from(
        select(Sessao).where(Sessao.user_id == user.id).subquery()
    )
    total_r = await db.execute(total_q)
    total = total_r.scalar() or 0

    q = q.offset((pagina - 1) * por_pagina).limit(por_pagina)
    result = await db.execute(q)
    sessoes = result.scalars().all()

    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "items": [
            {
                "sessao_id": s.external_id,
                "titulo": s.titulo or f"Relato {s.external_id[:8]}",
                "status": s.status,
                "care_score": s.care_score,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                "tem_docx": bool(s.docx_path and Path(s.docx_path).exists()),
            }
            for s in sessoes
        ],
    }


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Estatísticas do usuário para o dashboard."""
    result = await db.execute(
        select(Sessao).where(Sessao.user_id == user.id)
    )
    sessoes = result.scalars().all()

    total = len(sessoes)
    concluidos = [s for s in sessoes if s.status == "concluido"]
    em_andamento = [s for s in sessoes if s.status in ("gerando", "redigindo", "revisando", "finalizando")]
    com_erro = [s for s in sessoes if s.status == "erro"]

    scores = [s.care_score for s in concluidos if s.care_score is not None]
    media_care = round(sum(scores) / len(scores), 1) if scores else None

    return {
        "total_relatos": total,
        "concluidos": len(concluidos),
        "em_andamento": len(em_andamento),
        "com_erro": len(com_erro),
        "media_care_score": media_care,
        "artigos_mes": user.artigos_mes,
        "plano": user.plano,
    }


@router.patch("/{sessao_id}")
async def atualizar_sessao(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Atualiza título do relato."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    if "titulo" in body:
        sessao.titulo = str(body["titulo"])[:120]
    await db.commit()
    return {"sessao_id": sessao_id, "titulo": sessao.titulo}


@router.delete("/{sessao_id}")
async def deletar_sessao(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Remove um relato do histórico (e arquivo DOCX se existir)."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # Remove arquivo DOCX se existir
    if sessao.docx_path and Path(sessao.docx_path).exists():
        try:
            os.remove(sessao.docx_path)
        except OSError:
            pass

    await db.delete(sessao)
    await db.commit()
    return {"deletado": True, "sessao_id": sessao_id}


@router.get("/{sessao_id}/status", response_model=StatusResponse)
async def status_sessao(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id and sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    return StatusResponse(
        sessao_id=sessao_id,
        status=sessao.status,
        resultado=sessao.relatorio if sessao.status == "concluido" else None,
    )


@router.get("/{sessao_id}/resultado")
async def download_resultado(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()

    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id and sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if sessao.status != "concluido":
        raise HTTPException(status_code=425,
                            detail=f"Artigo ainda não concluído. Status: {sessao.status}")
    if not sessao.docx_path or not Path(sessao.docx_path).exists():
        raise HTTPException(status_code=404, detail="Arquivo DOCX não encontrado.")

    return FileResponse(
        path=sessao.docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"RCCS_{sessao_id}.docx",
    )


@router.post("/{sessao_id}/confirmar-flags")
async def confirmar_flags(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id and sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    return {"sessao_id": sessao_id, "confirmado": True}


@router.get("/pubmed/buscar")
async def buscar_pubmed(
    q: str = Query(..., min_length=3, max_length=200),
    max: int = Query(default=10, ge=1, le=20),
    user: Usuario = Depends(get_verified_user),
):
    """Busca artigos no PubMed e retorna lista com links diretos."""
    from app.utils.pubmed import pesquisar_pubmed

    try:
        artigos = await pesquisar_pubmed([q], max_por_query=max, max_total=max)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro na busca PubMed: {str(exc)[:200]}")

    def _pubmed_url(art: dict) -> str:
        if art.get("pmid"):
            return f"https://pubmed.ncbi.nlm.nih.gov/{art['pmid']}"
        if art.get("doi"):
            return f"https://doi.org/{art['doi']}"
        return ""

    return {
        "query": q,
        "total": len(artigos),
        "artigos": [
            {
                "pmid":       art.get("pmid", ""),
                "titulo":     art.get("titulo", "Sem título"),
                "autores":    art.get("autores", ""),
                "periodico":  art.get("periodico", ""),
                "ano":        art.get("ano", ""),
                "abstract":   (art.get("abstract") or "")[:500],
                "doi":        art.get("doi", ""),
                "url_pubmed": _pubmed_url(art),
                "citacoes":   art.get("citation_count") or art.get("citacoes", 0),
            }
            for art in artigos
        ],
    }

