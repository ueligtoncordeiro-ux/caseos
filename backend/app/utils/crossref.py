"""
Crossref API — validação de DOIs e metadados de referências.
Uso crítico: garante que nenhum DOI inventado pelo LLM entre no artigo final.
Polite pool: adiciona mailto ao User-Agent para rate limit generoso (50 req/s).
"""
import asyncio
import httpx
from typing import Optional,  Any

from app.config import settings

_BASE = "https://api.crossref.org"
_RATE = asyncio.Semaphore(5)

def _headers() -> dict:
    email = getattr(settings, "polite_email", "") or "rccs@exemplo.com"
    return {"User-Agent": f"RCCS/1.0 (mailto:{email})"}


async def _get(client: httpx.AsyncClient, url: str, params: dict = {}) -> dict:
    async with _RATE:
        resp = await client.get(url, params=params, headers=_headers(), timeout=15)
        resp.raise_for_status()
        await asyncio.sleep(0.1)
        return resp.json()


async def validar_doi(doi: str) -> dict[str, Any]:
    """
    Verifica se um DOI existe na Crossref.
    Retorna: {valido, titulo, autores, periodico, ano, doi}
    """
    if not doi or not doi.strip():
        return {"valido": False, "doi": doi}

    doi_limpo = doi.strip().lstrip("https://doi.org/").lstrip("doi:")
    url = f"{_BASE}/works/{doi_limpo}"

    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, url)
            msg = data.get("message", {})

            autores_raw = msg.get("author", [])
            autores = []
            for a in autores_raw[:6]:
                nome = f"{a.get('family', '')} {a.get('given', '')[:1]}".strip()
                if nome:
                    autores.append(nome)
            if len(autores_raw) > 6:
                autores.append("et al")

            titulo_raw = msg.get("title", [""])
            titulo = titulo_raw[0] if titulo_raw else ""

            journal_raw = msg.get("container-title", [""])
            journal = journal_raw[0] if journal_raw else ""

            date_parts = msg.get("published", {}).get("date-parts", [[""]])
            ano = str(date_parts[0][0]) if date_parts and date_parts[0] else ""

            volume  = msg.get("volume", "")
            numero  = msg.get("issue", "")
            paginas = msg.get("page", "")

            return {
                "valido":    True,
                "doi":       doi_limpo,
                "titulo":    titulo.rstrip("."),
                "autores":   ", ".join(autores),
                "periodico": journal,
                "ano":       ano,
                "volume":    volume,
                "numero":    numero,
                "paginas":   paginas,
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Only a definitive 404 means the DOI doesn't exist
                return {"valido": False, "doi": doi, "motivo": "DOI não encontrado na Crossref"}
            # Any other HTTP error (rate-limit, server error) → treat as uncertain, keep article
            return {"valido": True, "doi": doi, "motivo": f"Erro HTTP {e.response.status_code} — mantido por precaução"}
        except Exception as e:
            # Network errors, timeouts → keep the article (do not punish valid papers)
            return {"valido": True, "doi": doi, "motivo": f"Erro de rede — mantido por precaução: {e}"}


async def validar_lote(dois: list[str]) -> dict[str, dict]:
    """Valida múltiplos DOIs em paralelo (máx 10 simultâneos)."""
    sem = asyncio.Semaphore(10)

    async def _validar_one(doi: str) -> tuple[str, dict]:
        async with sem:
            return doi, await validar_doi(doi)

    resultados = await asyncio.gather(*[_validar_one(d) for d in dois])
    return dict(resultados)


async def buscar(
    query: str,
    filtros: Optional[dict] = None,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """
    Busca artigos na Crossref por termos livres.
    Útil para completar gaps que PubMed e S2 não cobrem.
    """
    params: dict[str, Any] = {
        "query": query,
        "rows": max_results,
        "select": "DOI,title,author,container-title,published,volume,issue,page,type",
        "filter": "type:journal-article",
    }
    email = getattr(settings, "polite_email", "") or "rccs@exemplo.com"
    params["mailto"] = email

    if filtros:
        params.update(filtros)

    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_BASE}/works", params)
            items = data.get("message", {}).get("items", [])
            resultado = []
            for item in items:
                autores_raw = item.get("author", [])
                autores = []
                for a in autores_raw[:6]:
                    nome = f"{a.get('family', '')} {a.get('given', '')[:1]}".strip()
                    if nome:
                        autores.append(nome)
                if len(autores_raw) > 6:
                    autores.append("et al")

                titulo_raw = item.get("title", [""])
                titulo = titulo_raw[0] if titulo_raw else ""
                if not titulo:
                    continue

                journal_raw = item.get("container-title", [""])
                journal = journal_raw[0] if journal_raw else ""

                date_parts = item.get("published", {}).get("date-parts", [[""]])
                ano = str(date_parts[0][0]) if date_parts and date_parts[0] else ""

                resultado.append({
                    "doi":            item.get("DOI", ""),
                    "titulo":         titulo.rstrip("."),
                    "autores":        ", ".join(autores),
                    "periodico":      journal,
                    "periodico_abrev": journal,
                    "ano":            ano,
                    "volume":         item.get("volume", ""),
                    "numero":         item.get("issue", ""),
                    "paginas":        item.get("page", ""),
                    "abstract":       "",
                    "pmid":           "",
                    "citation_count": 0,
                    "fonte":          "Crossref",
                })
            return resultado
        except Exception:
            return []
