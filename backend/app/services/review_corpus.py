import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import RevisaoFonte, RevisaoFonteChunk

_MAX_CHARS_CHUNK = 1200
_MIN_CHARS_CHUNK = 120


def _limpar_texto(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip()


def _texto_fonte(fonte: RevisaoFonte) -> str:
    blocos = [
        fonte.titulo or "",
        fonte.abstract or "",
    ]
    return _limpar_texto(" ".join(blocos))


def _chunk_texto(texto: str, max_chars: int = _MAX_CHARS_CHUNK) -> list[str]:
    texto = _limpar_texto(texto)
    if not texto:
        return []

    frases = re.split(r"(?<=[.!?])\s+", texto)
    chunks: list[str] = []
    atual = ""
    for frase in frases:
        frase = frase.strip()
        if not frase:
            continue
        if len(atual) + len(frase) + 1 <= max_chars:
            atual = f"{atual} {frase}".strip()
            continue
        if len(atual) >= _MIN_CHARS_CHUNK:
            chunks.append(atual)
            atual = frase
        else:
            chunks.append((atual + " " + frase).strip())
            atual = ""

    if atual:
        chunks.append(atual)
    return chunks


def _metadados_chunk(fonte: RevisaoFonte) -> dict[str, Any]:
    return {
        "titulo": fonte.titulo,
        "autores": fonte.autores,
        "ano": fonte.ano,
        "periodico": fonte.periodico,
        "doi": fonte.doi,
        "pmid": fonte.pmid,
        "url": fonte.url,
        "fonte_base": fonte.fonte_base,
        "origem": fonte.origem,
    }


async def reconstruir_corpus_fechado(
    revisao_id: str,
    db: AsyncSession,
    somente_aprovadas: bool = True,
) -> dict[str, Any]:
    all_sources_result = await db.execute(
        select(RevisaoFonte.id).where(RevisaoFonte.revisao_id == revisao_id)
    )
    todos_ids = list(all_sources_result.scalars().all())
    if todos_ids:
        await db.execute(
            delete(RevisaoFonteChunk).where(RevisaoFonteChunk.fonte_id.in_(todos_ids))
        )

    filtros = [RevisaoFonte.revisao_id == revisao_id]
    if somente_aprovadas:
        filtros.append(RevisaoFonte.aprovada_para_escrita.is_(True))

    result = await db.execute(
        select(RevisaoFonte)
        .where(*filtros)
        .order_by(RevisaoFonte.fonte_base, RevisaoFonte.ano.desc(), RevisaoFonte.titulo)
    )
    fontes = result.scalars().all()

    chunks_criados: list[RevisaoFonteChunk] = []
    fontes_sem_texto: list[str] = []
    for fonte in fontes:
        texto = _texto_fonte(fonte)
        partes = _chunk_texto(texto)
        if not partes:
            fontes_sem_texto.append(fonte.id)
            continue

        for ordem, parte in enumerate(partes, start=1):
            chunk = RevisaoFonteChunk(
                fonte_id=fonte.id,
                ordem=ordem,
                secao="abstract" if fonte.abstract else "metadados",
                texto=parte,
                embedding=None,
                metadados=_metadados_chunk(fonte),
            )
            db.add(chunk)
            chunks_criados.append(chunk)

    await db.commit()
    return {
        "fontes_processadas": len(fontes),
        "chunks_criados": len(chunks_criados),
        "fontes_sem_texto": fontes_sem_texto,
        "somente_aprovadas": somente_aprovadas,
    }
