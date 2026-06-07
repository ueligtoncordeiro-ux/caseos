"""
Europe PMC API — cobertura de artigos financiados por agências europeias e NIH.
Grátis, sem chave. Polite pool via email param.

Diferenciais vs PubMed:
  • Full text OA disponível via PMC para muitos artigos
  • Melhor cobertura de revistas de acesso aberto europeias
  • Inclui preprints (bioRxiv, medRxiv) — filtrados por padrão
  • API única que agrega MEDLINE + PMC + PPR + AGR + PAT + ETH

Ref: https://europepmc.org/RestfulWebService
"""
import asyncio
import httpx
from typing import Any, Optional

from app.config import settings

_BASE   = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_RATE   = asyncio.Semaphore(4)   # generoso, mas respeitoso
_EMAIL  = getattr(settings, "polite_email", None) or "caseos@exemplo.com"


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    async with _RATE:
        resp = await client.get(url, params=params, timeout=20)
        resp.raise_for_status()
        await asyncio.sleep(0.2)
        return resp.json()


def _normalizar(item: dict) -> dict[str, Any]:
    """Converte um resultado da Europe PMC para o formato interno do CaseOS."""
    # Autores
    autor_str: str = item.get("authorString", "") or ""
    # Europe PMC retorna "Autor A, Autor B, Autor C et al." — limpa o ponto final
    autor_str = autor_str.rstrip(".")

    # Periódico e dados bibliográficos
    journal_info: dict = item.get("journalInfo", {}) or {}
    journal_title: str = (
        item.get("journalTitle")
        or journal_info.get("journal", {}).get("title", "")
        or ""
    )
    journal_abbrev: str = (
        journal_info.get("journal", {}).get("isoabbreviation", "")
        or journal_title
    )
    volume: str  = journal_info.get("volume", "") or ""
    numero: str  = journal_info.get("issue",  "") or ""
    ano: str     = str(journal_info.get("yearOfPublication") or item.get("pubYear") or "")

    # IDs
    pmid: str = str(item.get("pmid") or "").strip()
    doi:  str = (item.get("doi")  or "").strip()
    pmcid: str = (item.get("pmcid") or "").strip()

    # OA URL — preferir PMC quando disponível
    full_text_url = ""
    if pmcid:
        full_text_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
    else:
        for ft in (item.get("fullTextUrlList", {}) or {}).get("fullTextUrl", []):
            if ft.get("availabilityCode") == "OA":
                full_text_url = ft.get("url", "")
                break

    # Abstract
    abstract: str = (item.get("abstractText") or "")[:500]

    return {
        "pmid":             pmid,
        "pmcid":            pmcid,
        "doi":              doi,
        "titulo":           (item.get("title") or "").rstrip("."),
        "autores":          autor_str,
        "periodico":        journal_title,
        "periodico_abrev":  journal_abbrev,
        "ano":              ano,
        "volume":           volume,
        "numero":           numero,
        "paginas":          (item.get("pageInfo") or "").strip(),
        "abstract":         abstract,
        "citation_count":   int(item.get("citedByCount") or 0),
        "open_access_url":  full_text_url,
        "is_open_access":   item.get("isOpenAccess") == "Y",
        "fonte":            "Europe PMC",
    }


async def buscar(
    query: str,
    max_results: int = 12,
    ano_min: int = 2010,
    apenas_oa: bool = False,
) -> list[dict[str, Any]]:
    """
    Busca artigos no Europe PMC.
    Exclui preprints (source != PPR) e limita a artigos revisados por pares.

    Args:
        query:       Texto livre ou query estruturada
        max_results: Máximo de resultados
        ano_min:     Ano mínimo de publicação (filtro FIRST_PDATE)
        apenas_oa:   Se True, retorna apenas artigos com texto completo disponível
    """
    # Garante que só retorna artigos revisados por pares (exclui PPR = preprints)
    filtro_src = "SRC:MED OR SRC:PMC OR SRC:AGR OR SRC:CBA"
    filtro_oa  = " AND OPEN_ACCESS:Y" if apenas_oa else ""
    q_final    = f"({query}) AND ({filtro_src}){filtro_oa} AND FIRST_PDATE:[{ano_min}-01-01 TO 9999-12-31]"

    params = {
        "query":      q_final,
        "format":     "json",
        "pageSize":   min(max_results, 25),  # API limita a 1 000, usamos 25 max
        "resultType": "core",                # inclui abstract e fullTextUrlList
        "sort":       "CITED desc",
        "email":      _EMAIL,
    }

    async with httpx.AsyncClient() as client:
        try:
            data   = await _get(client, f"{_BASE}/search", params)
            items  = (data.get("resultList") or {}).get("result", [])
            return [_normalizar(i) for i in items if i.get("title")]
        except Exception:
            return []


async def buscar_por_pmid(pmid: str) -> Optional[dict[str, Any]]:
    """Recupera metadados completos de um artigo pelo PMID."""
    params = {
        "query":      f"EXT_ID:{pmid} AND SRC:MED",
        "format":     "json",
        "pageSize":   1,
        "resultType": "core",
        "email":      _EMAIL,
    }
    async with httpx.AsyncClient() as client:
        try:
            data  = await _get(client, f"{_BASE}/search", params)
            items = (data.get("resultList") or {}).get("result", [])
            return _normalizar(items[0]) if items else None
        except Exception:
            return None


async def enriquecer_com_pmc(artigos: list[dict]) -> list[dict]:
    """
    Para artigos já presentes no pool (via PubMed etc.) que não têm open_access_url,
    tenta recuperar o PMCID via Europe PMC para adicionar o link OA.
    Opera apenas nos primeiros 20 artigos sem URL para não sobrecarregar a API.
    """
    sem = asyncio.Semaphore(3)

    async def _enriquecer_um(art: dict) -> None:
        if art.get("open_access_url") or not art.get("pmid"):
            return
        async with sem:
            resultado = await buscar_por_pmid(art["pmid"])
            if resultado and resultado.get("open_access_url"):
                art["open_access_url"] = resultado["open_access_url"]
                art["pmcid"]           = resultado.get("pmcid", "")

    candidatos = [a for a in artigos if not a.get("open_access_url")][:20]
    await asyncio.gather(*[_enriquecer_um(a) for a in candidatos])
    return artigos


async def executar_busca(
    query: str,
    diagnostico: str,
    ano_min: int = 2010,
) -> list[dict[str, Any]]:
    """Pipeline Europe PMC para o agente bibliográfico."""
    # Busca 1: query principal
    resultados = await buscar(query, max_results=12, ano_min=ano_min)

    # Busca 2: diagnóstico em inglês se diferente da query
    if diagnostico and diagnostico.lower() not in query.lower():
        r2 = await buscar(diagnostico, max_results=8, ano_min=ano_min)
        dois_vistos = {r["doi"] for r in resultados if r.get("doi")}
        pmids_vistos = {r["pmid"] for r in resultados if r.get("pmid")}
        for r in r2:
            if r.get("doi") not in dois_vistos and r.get("pmid") not in pmids_vistos:
                resultados.append(r)

    return resultados
