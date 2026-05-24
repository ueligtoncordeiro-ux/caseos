from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    Revisao,
    RevisaoDecisao,
    RevisaoFonte,
    Usuario,
    get_db,
)
from app.models.schemas import (
    RevisaoBuscarFontesRequest,
    RevisaoCreateRequest,
    RevisaoFonteCreateRequest,
    RevisaoFonteDecisaoRequest,
    RevisaoFontePublica,
    RevisaoImportarFontesRequest,
    RevisaoPublica,
    RevisaoUpdateRequest,
)
from app.services.auth import get_verified_user
from app.services.scientific_search import buscar_literatura

router = APIRouter(prefix="/revisoes", tags=["revisoes"])


async def _get_revisao_ou_404(
    revisao_id: str,
    user_id: str,
    db: AsyncSession,
) -> Revisao:
    result = await db.execute(
        select(Revisao).where(
            Revisao.id == revisao_id,
            Revisao.user_id == user_id,
        )
    )
    revisao = result.scalar_one_or_none()
    if not revisao:
        raise HTTPException(status_code=404, detail="Revisão não encontrada.")
    return revisao


async def _get_fonte_ou_404(
    revisao_id: str,
    fonte_id: str,
    user_id: str,
    db: AsyncSession,
) -> RevisaoFonte:
    await _get_revisao_ou_404(revisao_id, user_id, db)
    result = await db.execute(
        select(RevisaoFonte).where(
            RevisaoFonte.id == fonte_id,
            RevisaoFonte.revisao_id == revisao_id,
        )
    )
    fonte = result.scalar_one_or_none()
    if not fonte:
        raise HTTPException(status_code=404, detail="Fonte não encontrada.")
    return fonte


def _fonte_key(artigo: dict) -> str:
    doi = (artigo.get("doi") or "").lower().strip()
    pmid = (artigo.get("pmid") or "").strip()
    titulo = (artigo.get("titulo") or "").lower().strip()
    return doi or (f"pmid:{pmid}" if pmid else titulo)


def _fonte_payload(revisao_id: str, artigo: dict, query: str) -> RevisaoFonte:
    fonte = artigo.get("fonte") or ("PubMed" if artigo.get("pmid") else "")
    return RevisaoFonte(
        revisao_id=revisao_id,
        origem="busca",
        fonte_base=fonte,
        titulo=artigo.get("titulo") or "Sem título",
        autores=artigo.get("autores") or None,
        ano=str(artigo.get("ano") or "") or None,
        periodico=artigo.get("periodico") or None,
        doi=artigo.get("doi") or None,
        pmid=artigo.get("pmid") or None,
        url=artigo.get("url") or artigo.get("url_pubmed") or artigo.get("open_access_url") or None,
        abstract=artigo.get("abstract") or None,
        metadados={
            "query": query,
            "citacoes": artigo.get("citacoes", 0),
            "open_access_url": artigo.get("open_access_url") or "",
            "openalex_id": artigo.get("openalex_id") or "",
            "s2_id": artigo.get("s2_id") or "",
        },
        tags=[fonte.lower().replace(" ", "_")] if fonte else [],
    )


async def _importar_artigos(
    revisao_id: str,
    artigos: list[dict],
    query: str,
    db: AsyncSession,
) -> tuple[list[RevisaoFonte], int]:
    result = await db.execute(
        select(RevisaoFonte).where(RevisaoFonte.revisao_id == revisao_id)
    )
    existentes = {_fonte_key({
        "doi": fonte.doi,
        "pmid": fonte.pmid,
        "titulo": fonte.titulo,
    }) for fonte in result.scalars().all()}

    importadas: list[RevisaoFonte] = []
    duplicadas = 0
    vistas_lote: set[str] = set()
    for artigo in artigos:
        chave = _fonte_key(artigo)
        if not chave or chave in existentes or chave in vistas_lote:
            duplicadas += 1
            continue
        vistas_lote.add(chave)
        fonte = _fonte_payload(revisao_id, artigo, query)
        db.add(fonte)
        importadas.append(fonte)

    if importadas:
        await db.commit()
        for fonte in importadas:
            await db.refresh(fonte)

    return importadas, duplicadas


@router.post("", response_model=RevisaoPublica, status_code=status.HTTP_201_CREATED)
async def criar_revisao(
    body: RevisaoCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    revisao = Revisao(
        user_id=user.id,
        tipo=body.tipo,
        titulo=body.titulo,
        tema=body.tema,
        pergunta=body.pergunta,
        objetivo=body.objetivo,
        formato_ref=body.formato_ref,
        protocolo={},
        criterios={},
        preferencias={},
        checklist={},
    )
    db.add(revisao)
    await db.commit()
    await db.refresh(revisao)
    return revisao


@router.get("")
async def listar_revisoes(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=10, ge=1, le=50),
    status_filtro: Optional[str] = Query(default=None, alias="status"),
    tipo: Optional[str] = Query(default=None),
):
    filtros = [Revisao.user_id == user.id]
    if status_filtro:
        filtros.append(Revisao.status == status_filtro)
    if tipo:
        filtros.append(Revisao.tipo == tipo)

    total_result = await db.execute(select(func.count()).select_from(Revisao).where(*filtros))
    total = total_result.scalar() or 0

    result = await db.execute(
        select(Revisao)
        .where(*filtros)
        .order_by(desc(Revisao.updated_at), desc(Revisao.created_at))
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    )
    revisoes = result.scalars().all()

    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "items": [RevisaoPublica.model_validate(revisao) for revisao in revisoes],
    }


@router.get("/{revisao_id}", response_model=RevisaoPublica)
async def obter_revisao(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    return await _get_revisao_ou_404(revisao_id, user.id, db)


@router.patch("/{revisao_id}", response_model=RevisaoPublica)
async def atualizar_revisao(
    revisao_id: str,
    body: RevisaoUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    revisao = await _get_revisao_ou_404(revisao_id, user.id, db)
    dados = body.model_dump(exclude_unset=True)
    for campo, valor in dados.items():
        setattr(revisao, campo, valor)

    await db.commit()
    await db.refresh(revisao)
    return revisao


@router.delete("/{revisao_id}")
async def deletar_revisao(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    revisao = await _get_revisao_ou_404(revisao_id, user.id, db)
    await db.delete(revisao)
    await db.commit()
    return {"deletado": True, "revisao_id": revisao_id}


@router.post(
    "/{revisao_id}/fontes",
    response_model=RevisaoFontePublica,
    status_code=status.HTTP_201_CREATED,
)
async def adicionar_fonte(
    revisao_id: str,
    body: RevisaoFonteCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    fonte = RevisaoFonte(
        revisao_id=revisao_id,
        origem=body.origem,
        fonte_base=body.fonte_base,
        titulo=body.titulo,
        autores=body.autores,
        ano=body.ano,
        periodico=body.periodico,
        doi=body.doi,
        pmid=body.pmid,
        url=body.url,
        abstract=body.abstract,
        metadados=body.metadados,
        tags=body.tags,
    )
    db.add(fonte)
    await db.commit()
    await db.refresh(fonte)
    return fonte


@router.post("/{revisao_id}/buscar-fontes")
async def buscar_fontes_para_revisao(
    revisao_id: str,
    body: RevisaoBuscarFontesRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    try:
        return await buscar_literatura(body.query, max_results=body.max, fontes=body.fontes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{revisao_id}/importar-fontes")
async def importar_fontes_para_revisao(
    revisao_id: str,
    body: RevisaoImportarFontesRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    artigos = body.artigos
    erros: list[str] = []

    if not artigos:
        try:
            resultado = await buscar_literatura(body.query, max_results=body.max, fontes=body.fontes)
            artigos = resultado["artigos"]
            erros = resultado.get("erros", [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    importadas, duplicadas = await _importar_artigos(revisao_id, artigos, body.query, db)
    return {
        "revisao_id": revisao_id,
        "query": body.query,
        "fontes": body.fontes,
        "importadas": len(importadas),
        "duplicadas_ignoradas": duplicadas,
        "erros": erros,
        "items": [RevisaoFontePublica.model_validate(fonte) for fonte in importadas],
    }


@router.get("/{revisao_id}/fontes")
async def listar_fontes(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=20, ge=1, le=100),
    decisao: Optional[str] = Query(default=None),
    origem: Optional[str] = Query(default=None),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    filtros = [RevisaoFonte.revisao_id == revisao_id]
    if decisao:
        filtros.append(RevisaoFonte.decisao_humana == decisao)
    if origem:
        filtros.append(RevisaoFonte.origem == origem)

    total_result = await db.execute(
        select(func.count()).select_from(RevisaoFonte).where(*filtros)
    )
    total = total_result.scalar() or 0

    result = await db.execute(
        select(RevisaoFonte)
        .where(*filtros)
        .order_by(desc(RevisaoFonte.aprovada_para_escrita), desc(RevisaoFonte.created_at))
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    )
    fontes = result.scalars().all()

    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "items": [RevisaoFontePublica.model_validate(fonte) for fonte in fontes],
    }


@router.patch(
    "/{revisao_id}/fontes/{fonte_id}/decisao",
    response_model=RevisaoFontePublica,
)
async def decidir_fonte(
    revisao_id: str,
    fonte_id: str,
    body: RevisaoFonteDecisaoRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    fonte = await _get_fonte_ou_404(revisao_id, fonte_id, user.id, db)
    fonte.decisao_humana = body.decisao
    fonte.motivo_decisao = body.motivo
    fonte.aprovada_para_escrita = body.decisao == "incluida"

    db.add(
        RevisaoDecisao(
            revisao_id=revisao_id,
            fonte_id=fonte.id,
            decisao=body.decisao,
            motivo=body.motivo,
            feita_por="humano",
        )
    )
    await db.commit()
    await db.refresh(fonte)
    return fonte
