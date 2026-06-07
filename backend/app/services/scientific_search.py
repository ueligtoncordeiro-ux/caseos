import asyncio
import re
from typing import Any, Optional


_STOPWORDS_BUSCA = {
    "and", "or", "e", "ou", "de", "da", "do", "das", "dos", "the", "a", "an",
    "of", "for", "in", "on", "with", "em", "com", "para", "por",
}

_PRIORIDADE_FONTE = {
    "PubMed": 0,
    "Semantic Scholar": 1,
    "OpenAlex": 2,
    "Crossref": 3,
}


def termos_relevantes(q: str) -> list[str]:
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


def pontuar_relevancia(art: dict[str, Any], termos: list[str]) -> int:
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


def url_artigo(art: dict[str, Any]) -> str:
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


def normalizar_artigo(art: dict[str, Any]) -> dict[str, Any]:
    return {
        "pmid":       art.get("pmid", ""),
        "doi":        art.get("doi", ""),
        "titulo":     art.get("titulo", "Sem título"),
        "autores":    art.get("autores", ""),
        "periodico":  art.get("periodico", ""),
        "ano":        art.get("ano", ""),
        "abstract":   (art.get("abstract") or "")[:500],
        "fonte":      art.get("fonte", "PubMed" if art.get("pmid") else ""),
        "url":        url_artigo(art),
        "url_pubmed": url_artigo(art),
        "citacoes":   art.get("citation_count") or art.get("citacoes", 0),
        "open_access_url": art.get("open_access_url", ""),
        "openalex_id": art.get("openalex_id", ""),
        "s2_id": art.get("s2_id", ""),
    }


def deduplicar_artigos(artigos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vistos: set[str] = set()
    resultado: list[dict[str, Any]] = []
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


async def buscar_literatura(
    query: str,
    max_results: int = 12,
    fontes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Busca literatura científica em múltiplas bases, com PubMed priorizado."""
    from app.utils.pubmed import pesquisar_pubmed
    from app.utils.semantic_scholar import buscar as buscar_s2
    from app.utils.openalex import buscar as buscar_openalex
    from app.utils.crossref import buscar as buscar_crossref

    fontes_norm = {f.lower() for f in (fontes or ["pubmed", "semantic", "openalex"])}
    query_busca = query.strip()
    termos = termos_relevantes(query_busca)
    tarefas = []

    if "pubmed" in fontes_norm:
        tarefas.append(pesquisar_pubmed([query_busca], max_por_query=max_results,
                                        max_total=max_results))
    if "semantic" in fontes_norm or "semantic_scholar" in fontes_norm:
        tarefas.append(buscar_s2(query_busca, max_results=max_results))
    if "openalex" in fontes_norm:
        tarefas.append(buscar_openalex(query_busca, max_results=max_results))
    if "crossref" in fontes_norm:
        tarefas.append(buscar_crossref(query_busca, max_results=max_results))

    if not tarefas:
        raise ValueError("Selecione pelo menos uma base de busca.")

    # Timeout global de 45 s — cada fonte tem seu próprio timeout por request
    # (15-30 s), mas múltiplas tentativas sequenciais dentro de uma fonte
    # poderiam exceder isso sem um cap global.
    try:
        resultados = await asyncio.wait_for(
            asyncio.gather(*tarefas, return_exceptions=True),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        resultados = [TimeoutError(f"Busca em {len(tarefas)} fonte(s) excedeu 45 s.")]
    artigos: list[dict[str, Any]] = []
    erros: list[str] = []
    for item in resultados:
        if isinstance(item, Exception):
            erros.append(str(item)[:160])
            continue
        artigos.extend(item)

    artigos = deduplicar_artigos(artigos)
    artigos = [art for art in artigos if pontuar_relevancia(art, termos) > 0]
    artigos = sorted(
        artigos,
        key=lambda a: (
            _PRIORIDADE_FONTE.get(a.get("fonte") or ("PubMed" if a.get("pmid") else ""), 9),
            -pontuar_relevancia(a, termos),
            -(a.get("citation_count") or a.get("citacoes") or 0),
            -(int(a.get("ano") or 0) if str(a.get("ano") or "").isdigit() else 0),
        ),
    )[:max_results]

    return {
        "query": query,
        "query_executada": query_busca,
        "fontes": sorted(fontes_norm),
        "total": len(artigos),
        "erros": erros,
        "artigos": [normalizar_artigo(art) for art in artigos],
    }
