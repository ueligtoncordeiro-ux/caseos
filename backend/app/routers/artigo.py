import os
import asyncio
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional, Any
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.models.schemas import CKO, IniciarResponse, StatusResponse
from app.models.database import PesquisaSalva, Sessao, get_db, Usuario
from app.agents.orchestrator import executar_pipeline, executar_pipeline_demo
from app.services.auth import get_verified_user, check_quota

router = APIRouter(prefix="/artigo", tags=["artigo"])

# ── Rate limiting simples para /demo (sem dependência externa) ────────────────
_DEMO_MAX_POR_HORA = 3          # máximo de demos por IP por hora
_DEMO_JANELA_SEG   = 3600       # janela de 1 hora
_demo_hits: dict[str, list[float]] = defaultdict(list)   # ip → [timestamps]


def _check_demo_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    agora = time.time()
    janela_inicio = agora - _DEMO_JANELA_SEG

    # Remove timestamps fora da janela
    _demo_hits[ip] = [t for t in _demo_hits[ip] if t > janela_inicio]

    if len(_demo_hits[ip]) >= _DEMO_MAX_POR_HORA:
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {_DEMO_MAX_POR_HORA} demos por hora atingido. "
                   "Crie uma conta gratuita para continuar.",
        )
    _demo_hits[ip].append(agora)


class SalvarPesquisaRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=300)
    operador: str = "AND"
    fontes: list[str] = Field(default_factory=list)
    artigos: list[dict[str, Any]] = Field(default_factory=list)
    tipo: str = Field(default="busca", pattern="^(busca|artigo|caso_clinico)$")
    observacao: Optional[str] = Field(default=None, max_length=500)


@router.post("/demo", response_model=IniciarResponse)
async def iniciar_demo(
    cko: CKO,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Pipeline de demonstração — sem autenticação. Gera apenas Introdução + Caso Clínico.
    Limitado a 3 demos/hora por IP para evitar abuso de LLM."""
    _check_demo_rate_limit(request)
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
        # Permite apenas se a sessão ainda não tem dono (prévia demo → upgrade para relato)
        # ou se o dono é o próprio usuário.
        # O `and` intencional: NULL significa sessão anônima de demo, que pode ser reivindicada.
        if existing.user_id is not None and existing.user_id != user.id:
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

    background_tasks.add_task(executar_pipeline, cko.sessao_id, cko, user.id, user.nome)

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

    total_paginas = max(1, (total + por_pagina - 1) // por_pagina)
    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "total_paginas": total_paginas,   # campo que o dashboard lê
        "paginas": total_paginas,          # alias de compatibilidade
        "itens": [                         # campo que o dashboard lê
            {
                "sessao_id": s.external_id,
                "titulo": s.titulo or f"Relato {s.external_id[:8]}",
                "status": s.status,
                "care_score": s.care_score,
                "criado_em": s.created_at.isoformat() if s.created_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                "tem_docx": bool(s.docx_path and Path(s.docx_path).exists()),
                "versao_edicao": (s.resultado or {}).get("versao_edicao", 1) if s.resultado else 1,
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
    # Verificação estrita: sessão sem dono (demo não reivindicada) também é negada.
    # Só o dono pode consultar o status de um relato.
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    return StatusResponse(
        sessao_id=sessao_id,
        status=sessao.status,
        resultado=sessao.relatorio if sessao.status == "concluido" else None,
    )


@router.get("/{sessao_id}")
async def detalhe_sessao(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Retorna dados completos da sessão (cko + resultado + relatorio) para visualização."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    return {
        "sessao_id": sessao.external_id,
        "titulo": sessao.titulo or f"Relato {sessao.external_id[:8]}",
        "status": sessao.status,
        "care_score": sessao.care_score,
        "criado_em": sessao.created_at.isoformat() if sessao.created_at else None,
        "cko": sessao.cko,
        "resultado": sessao.resultado,
        "relatorio": sessao.relatorio,
        "flags": sessao.flags,
        "tokens_usados": sessao.tokens_usados,
    }


@router.get("/{sessao_id}/resultado")
async def download_resultado(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    from app.config import settings
    from app.models.schemas import ArtigoGerado, CKO as CKOSchema
    from app.services.docx_generator import gerar_docx
    from fastapi.responses import FileResponse
    import tempfile

    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()

    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if sessao.status != "concluido":
        raise HTTPException(status_code=425,
                            detail=f"Artigo ainda não concluído. Status: {sessao.status}")

    # Se docx_path existe e o arquivo está no disco, serve diretamente
    if sessao.docx_path:
        docx_base = Path(settings.docx_output_dir).resolve()
        docx_path = Path(sessao.docx_path).resolve()
        if str(docx_path).startswith(str(docx_base)) and docx_path.exists():
            return FileResponse(
                path=str(docx_path),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=f"CaseOS_{sessao_id}.docx",
            )

    # Geração on-the-fly a partir do resultado JSON armazenado
    if not sessao.resultado:
        raise HTTPException(status_code=404,
                            detail="Conteúdo do artigo não encontrado.")

    try:
        import copy
        res = copy.deepcopy(sessao.resultado)

        # Normaliza campo caso_clinico (importações usam relato_caso)
        if "caso_clinico" not in res and "relato_caso" in res:
            res["caso_clinico"] = res.pop("relato_caso")

        # Normaliza resumo (importações usam "relato" em vez de "caso")
        if isinstance(res.get("resumo"), dict):
            rs = res["resumo"]
            if "caso" not in rs and "relato" in rs:
                rs["caso"] = rs.pop("relato")

        # Normaliza referências (podem ser strings ou dicts)
        refs_raw = res.get("referencias", [])
        refs_norm = []
        for i, ref in enumerate(refs_raw, start=1):
            if isinstance(ref, str):
                refs_norm.append({
                    "numero": i, "autores": "", "titulo": ref,
                    "periodico": "", "ano": "", "formatada": ref,
                })
            elif isinstance(ref, dict):
                if "formatada" not in ref:
                    ref["formatada"] = ref.get("titulo", "")
                refs_norm.append(ref)
        res["referencias"] = refs_norm

        artigo = ArtigoGerado(**res)

        # CKO — tenta parsear, usa mínimo como fallback
        cko_data = sessao.cko or {}
        try:
            cko = CKOSchema(**cko_data)
        except Exception:
            from app.models.schemas import (
                Identificacao, Historia, Achados, Diagnostico,
                Intervencao, Desfechos, Editorial,
                IntervencoesAnteriores, Timeline,
            )
            cko = CKOSchema(
                sessao_id=sessao_id,
                identificacao=Identificacao(),
                historia=Historia(queixa_principal="", hda=""),
                intervencoes_anteriores=IntervencoesAnteriores(),
                achados=Achados(exame_geral="", achados_especificos=""),
                timeline=Timeline(),
                diagnostico=Diagnostico(diagnostico_definitivo=""),
                intervencao=Intervencao(tipo="", descricao=""),
                desfechos=Desfechos(desfecho_clinico=""),
                editorial=Editorial(problemas_clinicos="", diferencial_caso=""),
            )

        docx_path = await gerar_docx(sessao_id, artigo, cko)

        # Persiste o path para evitar regerar sempre
        sessao.docx_path = docx_path
        await db.commit()

        return FileResponse(
            path=str(docx_path),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=f"CaseOS_{sessao_id}.docx",
        )
    except Exception as exc:
        import traceback, logging
        logging.getLogger(__name__).error(f"Erro ao gerar DOCX: {traceback.format_exc()}")
        raise HTTPException(status_code=500,
                            detail=f"Erro ao gerar DOCX: {str(exc)}")


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
    if sessao.user_id != user.id:
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


_STOPWORDS_BUSCA = {
    "and", "or", "e", "ou", "de", "da", "do", "das", "dos", "the", "a", "an",
    "of", "for", "in", "on", "with", "em", "com", "para", "por",
}


def _termos_relevantes(q: str) -> list[str]:
    termos = re.findall(r"[A-Za-zÀ-ÿ0-9]+", q.lower())
    return [t for t in termos if len(t) >= 3 and t not in _STOPWORDS_BUSCA]


def _variantes_termo(termo: str) -> set[str]:
    variantes = {termo}
    if termo.endswith("s") and len(termo) > 4:
        variantes.add(termo[:-1])
    if termo.endswith("ics") and len(termo) > 6:
        variantes.add(termo[:-3])
    if termo.endswith("ic") and len(termo) > 5:
        variantes.add(termo[:-2])
    return variantes


def _pontuar_relevancia(art: dict, termos: list[str]) -> int:
    if not termos:
        return 1

    titulo = (art.get("titulo") or "").lower()
    resumo = (art.get("abstract") or "").lower()
    periodico = (art.get("periodico") or "").lower()
    score = 0

    for termo in termos:
        variantes = _variantes_termo(termo)
        if any(v in titulo for v in variantes):
            score += 4
        if any(v in resumo for v in variantes):
            score += 2
        if any(v in periodico for v in variantes):
            score += 1
    return score


def _url_artigo(art: dict) -> str:
    if art.get("pmid"):
        return f"https://pubmed.ncbi.nlm.nih.gov/{art['pmid']}"
    if art.get("doi"):
        return f"https://doi.org/{art['doi']}"
    if art.get("open_access_url"):
        return art["open_access_url"]
    if art.get("s2_url"):
        return art["s2_url"]
    if art.get("openalex_id"):
        return art["openalex_id"]
    return ""


def _normalizar_artigo(art: dict) -> dict:
    return {
        "pmid":       art.get("pmid", ""),
        "doi":        art.get("doi", ""),
        "titulo":     art.get("titulo", "Sem título"),
        "autores":    art.get("autores", ""),
        "periodico":  art.get("periodico", ""),
        "ano":        art.get("ano", ""),
        "abstract":   (art.get("abstract") or "")[:500],
        "fonte":      art.get("fonte", "PubMed" if art.get("pmid") else ""),
        "url":        _url_artigo(art),
        "url_pubmed": _url_artigo(art),
        "citacoes":   art.get("citation_count") or art.get("citacoes", 0),
        "open_access_url": art.get("open_access_url", ""),
    }


def _deduplicar_artigos(artigos: list[dict]) -> list[dict]:
    vistos: set[str] = set()
    resultado: list[dict] = []
    for art in artigos:
        doi = (art.get("doi") or "").lower().strip()
        pmid = (art.get("pmid") or "").strip()
        titulo = (art.get("titulo") or "").lower().strip()
        chave = doi or (f"pmid:{pmid}" if pmid else titulo)
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        resultado.append(art)
    return resultado


@router.get("/biblioteca/buscar")
async def buscar_biblioteca(
    q: str = Query(..., min_length=3, max_length=300),
    max: int = Query(default=12, ge=1, le=30),
    fontes: list[str] = Query(default=["pubmed", "semantic", "openalex", "crossref"]),
    user: Usuario = Depends(get_verified_user),
):
    """Busca literatura em múltiplas bases e retorna resultados deduplicados."""
    from app.utils.pubmed import pesquisar_pubmed
    from app.utils.semantic_scholar import buscar as buscar_s2
    from app.utils.openalex import buscar as buscar_openalex
    from app.utils.crossref import buscar as buscar_crossref

    fontes_norm = {f.lower() for f in fontes}
    query_busca = q.strip()
    termos = _termos_relevantes(query_busca)
    tarefas = []

    if "pubmed" in fontes_norm:
        tarefas.append(pesquisar_pubmed([query_busca], max_por_query=max, max_total=max))
    if "semantic" in fontes_norm or "semantic_scholar" in fontes_norm:
        tarefas.append(buscar_s2(query_busca, max_results=max))
    if "openalex" in fontes_norm:
        tarefas.append(buscar_openalex(query_busca, max_results=max))
    if "crossref" in fontes_norm:
        tarefas.append(buscar_crossref(query_busca, max_results=max))

    if not tarefas:
        raise HTTPException(status_code=400, detail="Selecione pelo menos uma base de busca.")

    resultados = await asyncio.gather(*tarefas, return_exceptions=True)
    artigos: list[dict] = []
    erros: list[str] = []
    for item in resultados:
        if isinstance(item, Exception):
            erros.append(str(item)[:160])
            continue
        artigos.extend(item)

    artigos = _deduplicar_artigos(artigos)
    artigos = [
        art for art in artigos
        if _pontuar_relevancia(art, termos) > 0
    ]
    prioridade_fonte = {
        "PubMed": 0,
        "Semantic Scholar": 1,
        "OpenAlex": 2,
        "Crossref": 3,
    }
    artigos = sorted(
        artigos,
        key=lambda a: (
            prioridade_fonte.get(a.get("fonte") or ("PubMed" if a.get("pmid") else ""), 9),
            -_pontuar_relevancia(a, termos),
            -(a.get("citation_count") or a.get("citacoes") or 0),
            -(int(a.get("ano") or 0) if str(a.get("ano") or "").isdigit() else 0),
        ),
    )[:max]

    return {
        "query": q,
        "query_executada": query_busca,
        "fontes": sorted(fontes_norm),
        "total": len(artigos),
        "erros": erros,
        "artigos": [_normalizar_artigo(art) for art in artigos],
    }


@router.patch("/{sessao_id}/secao")
async def editar_secao(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Salva edição manual de uma seção do artigo."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if not sessao.resultado:
        raise HTTPException(status_code=400, detail="Artigo não gerado ainda.")

    secao = body.get("secao")
    conteudo = body.get("conteudo")
    SECOES_VALIDAS = {"introducao", "caso_clinico", "discussao", "conclusao", "palavras_chave", "resumo"}
    if secao not in SECOES_VALIDAS:
        raise HTTPException(status_code=400, detail=f"Seção inválida.")

    # Type validation per section
    if secao in {"introducao", "caso_clinico", "discussao", "conclusao"}:
        if not isinstance(conteudo, list):
            raise HTTPException(status_code=400, detail="conteudo deve ser uma lista de parágrafos.")
    elif secao == "palavras_chave":
        if not isinstance(conteudo, list):
            raise HTTPException(status_code=400, detail="conteudo deve ser uma lista de palavras.")
    elif secao == "resumo":
        if not isinstance(conteudo, dict):
            raise HTTPException(status_code=400, detail="conteudo deve ser um dicionário {introducao, caso, discussao, conclusao}.")

    resultado = dict(sessao.resultado)
    resultado[secao] = conteudo
    resultado["versao_edicao"] = resultado.get("versao_edicao", 1) + 1
    sessao.resultado = resultado
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(sessao, "resultado")
    await db.commit()
    return {"ok": True, "versao_edicao": resultado["versao_edicao"]}


@router.post("/{sessao_id}/melhorar-secao")
async def melhorar_secao(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Reescreve uma seção usando IA (consome tokens)."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if not sessao.resultado:
        raise HTTPException(status_code=400, detail="Artigo não gerado ainda.")

    secao = body.get("secao")
    conteudo_atual = body.get("conteudo_atual", "")
    instrucao = body.get("instrucao", "")
    SECOES_VALIDAS = {"introducao", "caso_clinico", "discussao", "conclusao"}
    if secao not in SECOES_VALIDAS:
        raise HTTPException(status_code=400, detail="Seção inválida.")

    from app.agents.editor import melhorar_secao_llm
    paragrafos = await melhorar_secao_llm(
        secao=secao,
        conteudo_atual=conteudo_atual,
        instrucao=instrucao,
        cko=sessao.cko or {},
    )

    resultado = dict(sessao.resultado)
    resultado[secao] = paragrafos
    resultado["versao_edicao"] = resultado.get("versao_edicao", 1) + 1
    sessao.resultado = resultado
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(sessao, "resultado")
    await db.commit()

    return {"ok": True, "conteudo": paragrafos, "versao_edicao": resultado["versao_edicao"]}


@router.post("/biblioteca/salvar")
async def salvar_pesquisa(
    body: SalvarPesquisaRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Salva uma busca ou artigo selecionado na biblioteca do usuário."""
    item = PesquisaSalva(
        user_id=user.id,
        tipo=body.tipo,
        query=body.query,
        operador=body.operador,
        fontes=body.fontes,
        artigos=body.artigos[:50],
        observacao=body.observacao,
    )
    db.add(item)
    await db.commit()
    return {"salvo": True, "id": item.id, "tipo": item.tipo}


@router.get("/biblioteca/artigos")
async def listar_artigos_salvos(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Lista artigos salvos pelo usuário (tipo artigo ou caso_clinico)."""
    result = await db.execute(
        select(PesquisaSalva)
        .where(PesquisaSalva.user_id == user.id)
        .where(PesquisaSalva.tipo.in_(["artigo", "caso_clinico"]))
        .order_by(desc(PesquisaSalva.created_at))
        .limit(100)
    )
    itens = result.scalars().all()
    artigos = []
    for item in itens:
        for art in (item.artigos or []):
            entry = dict(art)
            entry["tipo_salvo"] = item.tipo
            entry["observacao"] = item.observacao
            entry["salvo_em"] = item.created_at.isoformat() if item.created_at else None
            entry["pesquisa_id"] = item.id
            artigos.append(entry)
    return {"total": len(artigos), "artigos": artigos}


@router.post("/{sessao_id}/adicionar-referencia")
async def adicionar_referencia(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Adiciona uma referência bibliográfica ao resultado de um relato."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if not sessao.resultado:
        raise HTTPException(status_code=400, detail="Artigo não gerado ainda.")

    art = body.get("artigo", {})
    resultado = dict(sessao.resultado)
    refs = list(resultado.get("referencias", []))

    # Next reference number
    numeros = [r.get("numero", 0) if isinstance(r, dict) else 0 for r in refs]
    proximo = (max(numeros) + 1) if numeros else 1

    # Build Vancouver-formatted citation
    autores = art.get("autores") or ""
    titulo = art.get("titulo") or ""
    periodico = art.get("periodico_abrev") or art.get("periodico") or ""
    ano = art.get("ano") or ""
    volume = art.get("volume") or ""
    numero_ed = art.get("numero") or ""
    paginas = art.get("paginas") or ""
    doi = art.get("doi") or ""

    formatada = f"{autores}. {titulo.rstrip('.')}. {periodico}. {ano}"
    if volume: formatada += f";{volume}"
    if numero_ed: formatada += f"({numero_ed})"
    if paginas: formatada += f":{paginas}"
    formatada += "."
    if doi: formatada += f" doi: {doi}"

    nova_ref = {
        "numero": proximo,
        "autores": autores,
        "titulo": titulo,
        "periodico": periodico,
        "ano": ano,
        "volume": volume or None,
        "paginas": paginas or None,
        "doi": doi or None,
        "pmid": art.get("pmid") or None,
        "formatada": art.get("formatada") or formatada,
    }

    refs.append(nova_ref)
    resultado["referencias"] = refs
    resultado["versao_edicao"] = resultado.get("versao_edicao", 1) + 1
    sessao.resultado = resultado
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(sessao, "resultado")
    await db.commit()

    return {"ok": True, "numero": proximo, "ref": nova_ref, "versao_edicao": resultado["versao_edicao"]}
