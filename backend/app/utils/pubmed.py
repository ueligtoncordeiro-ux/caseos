import asyncio
import httpx
from typing import Any
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_RATE = asyncio.Semaphore(3)  # 3 req/s sem chave, 10 com chave


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _get(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    async with _RATE:
        if settings.pubmed_api_key:
            params["api_key"] = settings.pubmed_api_key
        resp = await client.get(url, params=params, timeout=30)
        resp.raise_for_status()
        await asyncio.sleep(0.4)
        return resp


async def buscar_pmids(query: str, max_results: int = 10) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance",
    }
    async with httpx.AsyncClient() as client:
        resp = await _get(client, f"{EUTILS_BASE}/esearch.fcgi", params)
        data = resp.json()
        return data.get("esearchresult", {}).get("idlist", [])


async def buscar_detalhes(pmids: list[str]) -> list[dict[str, Any]]:
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
        "rettype": "abstract",
    }
    async with httpx.AsyncClient() as client:
        resp = await _get(client, f"{EUTILS_BASE}/esummary.fcgi", params)
        data = resp.json()
        result = data.get("result", {})
        articles = []
        for pmid in pmids:
            art = result.get(pmid)
            if not art or art.get("error"):
                continue
            authors_raw = art.get("authors", [])
            authors = [a.get("name", "") for a in authors_raw[:6]]
            if len(authors_raw) > 6:
                authors.append("et al")
            articles.append({
                "pmid": pmid,
                "titulo": art.get("title", "").rstrip("."),
                "autores": ", ".join(authors),
                "periodico": art.get("fulljournalname", art.get("source", "")),
                "periodico_abrev": art.get("source", ""),
                "ano": art.get("pubdate", "")[:4],
                "volume": art.get("volume", ""),
                "numero": art.get("issue", ""),
                "paginas": art.get("pages", ""),
                "doi": next((
                    uid.get("value", "")
                    for uid in art.get("articleids", [])
                    if uid.get("idtype") == "doi"
                ), ""),
                "abstract": "",
            })
        return articles


async def buscar_abstract(pmid: str) -> str:
    params = {
        "db": "pubmed",
        "id": pmid,
        "retmode": "text",
        "rettype": "abstract",
    }
    async with httpx.AsyncClient() as client:
        resp = await _get(client, f"{EUTILS_BASE}/efetch.fcgi", params)
        text = resp.text
        # Extract abstract portion only
        if "Abstract" in text:
            parts = text.split("Abstract\n", 1)
            if len(parts) > 1:
                return parts[1].split("\n\n")[0].strip()
        return ""


async def pesquisar_pubmed(
    queries: list[str],
    max_por_query: int = 8,
    max_total: int = 20,
) -> list[dict[str, Any]]:
    pmids_vistos: set[str] = set()
    todos_artigos: list[dict] = []

    for query in queries:
        try:
            pmids = await buscar_pmids(query, max_por_query)
            novos = [p for p in pmids if p not in pmids_vistos]
            pmids_vistos.update(novos)
            artigos = await buscar_detalhes(novos)
            todos_artigos.extend(artigos)
            if len(todos_artigos) >= max_total:
                break
        except Exception:
            continue

    # Fetch abstracts for top articles (limit to avoid rate issues)
    for art in todos_artigos[:12]:
        if art.get("pmid") and not art.get("abstract"):
            try:
                art["abstract"] = await buscar_abstract(art["pmid"])
            except Exception:
                art["abstract"] = ""

    return todos_artigos[:max_total]
