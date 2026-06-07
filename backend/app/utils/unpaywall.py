"""
Unpaywall API — localiza versões gratuitas (open access) de artigos pelo DOI.
Gratuita. Requer apenas um email válido como parâmetro (polite pool).
Uso no pipeline: enriquece o pool com URLs de PDF — o Agente Redator
pode verificar texto completo em vez de apenas o abstract.
"""
import asyncio
import httpx
from typing import Any

from app.config import settings

_BASE = "https://api.unpaywall.org/v2"
_RATE = asyncio.Semaphore(5)


def _email() -> str:
    return settings.polite_email


async def _get(client: httpx.AsyncClient, url: str) -> dict:
    async with _RATE:
        resp = await client.get(url, params={"email": _email()}, timeout=15)
        resp.raise_for_status()
        await asyncio.sleep(0.2)
        return resp.json()


async def verificar_doi(doi: str) -> dict[str, Any]:
    """
    Verifica se um artigo tem versão open access.
    Retorna: {is_oa, pdf_url, oa_status, versao}
    """
    if not doi:
        return {"is_oa": False, "doi": doi}

    doi_limpo = doi.strip().lstrip("https://doi.org/")
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_BASE}/{doi_limpo}")
            best = data.get("best_oa_location") or {}
            return {
                "doi":       doi_limpo,
                "is_oa":     data.get("is_oa", False),
                "oa_status": data.get("oa_status", "closed"),
                "pdf_url":   best.get("url_for_pdf") or best.get("url") or "",
                "versao":    best.get("version", ""),
                "hospedeiro": best.get("host_type", ""),
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"is_oa": False, "doi": doi_limpo, "motivo": "DOI não encontrado"}
            return {"is_oa": False, "doi": doi_limpo}
        except Exception:
            return {"is_oa": False, "doi": doi_limpo}


async def verificar_lote(dois: list[str]) -> dict[str, dict]:
    """
    Verifica open access de múltiplos DOIs em paralelo.
    Retorna dict {doi: resultado}
    """
    sem = asyncio.Semaphore(8)

    async def _um(doi: str) -> tuple[str, dict]:
        async with sem:
            return doi, await verificar_doi(doi)

    resultados = await asyncio.gather(*[_um(d) for d in dois if d])
    return dict(resultados)


async def enriquecer_pool(artigos: list[dict]) -> list[dict]:
    """
    Recebe o pool de artigos e adiciona open_access_url via Unpaywall
    para os que têm DOI mas ainda não têm URL de acesso aberto.
    """
    dois_sem_url = [
        art["doi"] for art in artigos
        if art.get("doi") and not art.get("open_access_url")
    ]

    if not dois_sem_url:
        return artigos

    resultados = await verificar_lote(dois_sem_url)

    for art in artigos:
        doi = art.get("doi", "")
        if doi in resultados:
            oa = resultados[doi]
            if oa.get("is_oa") and oa.get("pdf_url"):
                art["open_access_url"] = oa["pdf_url"]
                art["oa_status"] = oa.get("oa_status", "")

    return artigos
