"""
Cliente Semantic Scholar Academic Graph API v1.
Estratégia de uso no pipeline:
  1. Enriquece PMIDs já encontrados pelo PubMed (citações, open access PDF)
  2. Busca independente por termos médicos com filtro fieldsOfStudy=Medicine
  3. Recomendações baseadas no artigo mais citado do pool PubMed
"""
import asyncio
import httpx
from typing import Any

from app.config import settings

_BASE = "https://api.semanticscholar.org/graph/v1"
_REC  = "https://api.semanticscholar.org/recommendations/v1"
_RATE = asyncio.Semaphore(1)   # 1 req/s sem chave, 10 com chave


def _headers() -> dict:
    if settings.semantic_scholar_api_key:
        return {"x-api-key": settings.semantic_scholar_api_key}
    return {}


async def _get(client: httpx.AsyncClient, url: str, params: dict = {}) -> dict:
    async with _RATE:
        resp = await client.get(url, params=params, headers=_headers(), timeout=20)
        resp.raise_for_status()
        if settings.semantic_scholar_api_key:
            await asyncio.sleep(0.15)
        else:
            await asyncio.sleep(1.1)  # respeita 1 req/s sem chave
        return resp.json()


async def _post(client: httpx.AsyncClient, url: str, body: dict, params: dict = {}) -> dict:
    async with _RATE:
        resp = await client.post(url, json=body, params=params, headers=_headers(), timeout=20)
        resp.raise_for_status()
        await asyncio.sleep(1.1 if not settings.semantic_scholar_api_key else 0.15)
        return resp.json()


# ── 1. Enriquecer por PMIDs ──────────────────────────────────────────────────

async def enriquecer_por_pmids(pmids: list[str]) -> dict[str, dict]:
    """
    Recebe lista de PMIDs, retorna dict {pmid: {citationCount, openAccessPdf, s2_url}}
    """
    if not pmids:
        return {}

    ids = [f"PMID:{p}" for p in pmids[:100]]
    fields = "externalIds,citationCount,openAccessPdf,url"
    enriched: dict[str, dict] = {}

    async with httpx.AsyncClient() as client:
        # Processar em batches de 50
        for i in range(0, len(ids), 50):
            batch = ids[i:i+50]
            try:
                data = await _post(
                    client,
                    f"{_BASE}/paper/batch",
                    {"ids": batch},
                    {"fields": fields},
                )
                for paper in data:
                    if not paper or paper.get("error"):
                        continue
                    ext = paper.get("externalIds") or {}
                    pmid = ext.get("PubMed")
                    if pmid:
                        pdf = paper.get("openAccessPdf") or {}
                        enriched[pmid] = {
                            "citation_count": paper.get("citationCount", 0),
                            "open_access_url": pdf.get("url", ""),
                            "s2_url": paper.get("url", ""),
                        }
            except Exception:
                continue

    return enriched


# ── 2. Busca independente ─────────────────────────────────────────────────────

_PAPER_FIELDS = "paperId,externalIds,title,abstract,authors,year,journal,citationCount,openAccessPdf"


async def buscar(
    query: str,
    ano_min: int = 2015,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "fields": _PAPER_FIELDS,
        "fieldsOfStudy": "Medicine",
        "year": f"{ano_min}-",
        "limit": max_results,
    }
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_BASE}/paper/search", params)
            return _normalizar(data.get("data", []))
        except Exception:
            return []


# ── 3. Recomendações ──────────────────────────────────────────────────────────

async def recomendar(s2_paper_id: str, max_results: int = 5) -> list[dict[str, Any]]:
    params = {"fields": _PAPER_FIELDS, "limit": max_results}
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_REC}/papers/forpaper/{s2_paper_id}", params)
            return _normalizar(data.get("recommendedPapers", []))
        except Exception:
            return []


# ── Normalização ──────────────────────────────────────────────────────────────

def _normalizar(papers: list[dict]) -> list[dict[str, Any]]:
    resultado = []
    for p in papers:
        if not p or not p.get("title"):
            continue
        ext = p.get("externalIds") or {}
        autores_raw = p.get("authors") or []
        autores = [a.get("name", "") for a in autores_raw[:6]]
        if len(autores_raw) > 6:
            autores.append("et al")
        journal = p.get("journal") or {}
        pdf = p.get("openAccessPdf") or {}
        resultado.append({
            "s2_id":          p.get("paperId", ""),
            "pmid":           ext.get("PubMed", ""),
            "doi":            ext.get("DOI", ""),
            "titulo":         p.get("title", "").rstrip("."),
            "autores":        ", ".join(autores),
            "ano":            str(p.get("year") or ""),
            "periodico":      journal.get("name", ""),
            "periodico_abrev": journal.get("name", ""),
            "volume":         journal.get("volume", ""),
            "paginas":        journal.get("pages", ""),
            "numero":         "",
            "abstract":       (p.get("abstract") or "")[:400],
            "citation_count": p.get("citationCount", 0),
            "open_access_url": pdf.get("url", ""),
            "fonte":          "Semantic Scholar",
        })
    return resultado


# ── Pipeline completo para o agente bibliográfico ─────────────────────────────

async def executar_busca(
    query_principal: str,
    pmids_pubmed: list[str],
    top_s2_id: str = "",
) -> tuple[list[dict], dict[str, dict]]:
    """
    Executa as 3 etapas em sequência e retorna:
      - artigos_s2: novos artigos encontrados pelo S2
      - enrichment: dados de enriquecimento dos PMIDs do PubMed
    """
    artigos_s2, enrichment = [], {}

    # Etapa 1: enriquecer PMIDs do PubMed
    if pmids_pubmed:
        enrichment = await enriquecer_por_pmids(pmids_pubmed)

    # Etapa 2: busca independente
    artigos_s2 = await buscar(query_principal, max_results=8)

    # Etapa 3: recomendações baseadas no paper mais citado
    if not top_s2_id and artigos_s2:
        mais_citado = max(artigos_s2, key=lambda x: x.get("citation_count", 0))
        top_s2_id = mais_citado.get("s2_id", "")

    if top_s2_id:
        recomendados = await recomendar(top_s2_id, max_results=4)
        # Adicionar apenas os que não duplicam por DOI/PMID
        pmids_existentes = {a.get("pmid") for a in artigos_s2 if a.get("pmid")}
        dois_existentes  = {a.get("doi")  for a in artigos_s2 if a.get("doi")}
        for r in recomendados:
            if r.get("pmid") in pmids_existentes or r.get("doi") in dois_existentes:
                continue
            artigos_s2.append(r)

    return artigos_s2, enrichment
