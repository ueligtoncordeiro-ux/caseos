"""
OpenAlex API — 250M+ artigos, cobertura excelente de medicina e odontologia.
Gratuita, sem chave. Polite pool via mailto param (rate limit generoso).
Diferencial: melhor cobertura de periódicos brasileiros vs PubMed/S2.
"""
import asyncio
import httpx
from typing import Optional, List, Any

from app.config import settings

_BASE = "https://api.openalex.org"
_RATE = asyncio.Semaphore(5)


def _params_base() -> dict:
    email = getattr(settings, "polite_email", "") or "rccs@exemplo.com"
    return {"mailto": email}


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    async with _RATE:
        all_params = {**_params_base(), **params}
        resp = await client.get(url, params=all_params, timeout=20)
        resp.raise_for_status()
        await asyncio.sleep(0.15)
        return resp.json()


def _reconstruir_abstract(inv_index: Optional[dict]) -> str:
    """OpenAlex armazena abstracts como índice invertido — reconstrói o texto."""
    if not inv_index:
        return ""
    try:
        palavras: dict[int, str] = {}
        for palavra, posicoes in inv_index.items():
            for pos in posicoes:
                palavras[pos] = palavra
        return " ".join(palavras[i] for i in sorted(palavras.keys()))
    except Exception:
        return ""


def _normalizar(work: dict) -> dict[str, Any]:
    autores_raw = work.get("authorships", [])
    autores = []
    for a in autores_raw[:6]:
        autor = a.get("author", {})
        nome = autor.get("display_name", "")
        if nome:
            partes = nome.split()
            sobrenome = partes[-1] if partes else nome
            iniciais = "".join(p[0] for p in partes[:-1]) if len(partes) > 1 else ""
            autores.append(f"{sobrenome} {iniciais}".strip())
    if len(autores_raw) > 6:
        autores.append("et al")

    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    journal = source.get("display_name", "")

    ids = work.get("ids", {})
    doi_raw = work.get("doi", "") or ids.get("doi", "") or ""
    doi = doi_raw.replace("https://doi.org/", "").strip()

    pmid_raw = ids.get("pmid", "") or ""
    pmid = pmid_raw.replace("https://pubmed.ncbi.nlm.nih.gov/", "").strip("/")

    oa = work.get("open_access", {})
    oa_url = oa.get("oa_url", "") or ""

    bib = work.get("biblio", {})

    return {
        "openalex_id":    work.get("id", ""),
        "doi":            doi,
        "pmid":           pmid,
        "titulo":         (work.get("title") or "").rstrip("."),
        "autores":        ", ".join(autores),
        "ano":            str(work.get("publication_year") or ""),
        "periodico":      journal,
        "periodico_abrev": journal,
        "volume":         bib.get("volume", ""),
        "numero":         bib.get("issue", ""),
        "paginas":        f"{bib.get('first_page','')}–{bib.get('last_page','')}".strip("–"),
        "abstract":       _reconstruir_abstract(work.get("abstract_inverted_index"))[:400],
        "citation_count": work.get("cited_by_count", 0),
        "open_access_url": oa_url,
        "fonte":          "OpenAlex",
    }


async def buscar(
    query: str,
    ano_min: int = 2015,
    max_results: int = 10,
    areas: Optional[List[str]] = None,
) -> List[dict]:
    """
    Busca artigos no OpenAlex.
    areas: ex. ["medicine", "dentistry"] — filtra por conceitos do OpenAlex
    """
    params: dict[str, Any] = {
        "search": query,
        "filter": f"publication_year:>{ano_min - 1},type:article",
        "sort": "cited_by_count:desc",
        "per-page": max_results,
        "select": "id,doi,title,authorships,publication_year,primary_location,"
                  "biblio,cited_by_count,open_access,abstract_inverted_index,ids",
    }

    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_BASE}/works", params)
            works = data.get("results", [])
            return [_normalizar(w) for w in works if w.get("title")]
        except Exception:
            return []


async def buscar_por_doi(doi: str) -> Optional[dict]:
    """Recupera metadados completos de um trabalho pelo DOI."""
    doi_limpo = doi.strip().lstrip("https://doi.org/")
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_BASE}/works/doi:{doi_limpo}", {})
            return _normalizar(data)
        except Exception:
            return None


async def buscar_citantes(openalex_id: str, max_results: int = 5) -> list[dict]:
    """Retorna artigos que citam o trabalho — útil para encontrar literatura mais recente."""
    params = {
        "filter": f"cites:{openalex_id},type:article",
        "sort": "publication_year:desc",
        "per-page": max_results,
        "select": "id,doi,title,authorships,publication_year,primary_location,"
                  "biblio,cited_by_count,open_access,abstract_inverted_index,ids",
    }
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_BASE}/works", params)
            return [_normalizar(w) for w in data.get("results", []) if w.get("title")]
        except Exception:
            return []


async def executar_busca(
    query: str,
    diagnostico: str,
    ano_min: int = 2015,
) -> list[dict[str, Any]]:
    """Pipeline completo OpenAlex para o agente bibliográfico."""
    # Busca 1: query principal
    resultados = await buscar(query, ano_min=ano_min, max_results=10)

    # Busca 2: diagnóstico específico para garantir cobertura
    if diagnostico and diagnostico.lower() not in query.lower():
        resultados2 = await buscar(diagnostico, ano_min=ano_min, max_results=8)
        dois_vistos = {r["doi"] for r in resultados if r.get("doi")}
        for r in resultados2:
            if r.get("doi") not in dois_vistos:
                resultados.append(r)

    return resultados
