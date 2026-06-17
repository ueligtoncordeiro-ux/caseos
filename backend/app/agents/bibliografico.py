"""
Agente Bibliográfico — pipeline 7 fontes:

  1. LLM constrói 5 queries otimizadas
  2. PubMed      → base primária (35M artigos biomédicos)
  3. Semantic S2 → enriquece PMIDs + busca complementar (200M artigos)
  4. OpenAlex    → busca complementar (250M artigos, melhor cobertura BR)
  5. Crossref    → busca direta + valida DOIs (anti-alucinação, só remove 404)
  6. Europe PMC  → cobertura adicional OA + enriquece PMCIDs sem URL
  7. Unpaywall   → adiciona PDF open access ao pool

Pool final: deduplicado por DOI/PMID, ordenado por citações, máx 40 artigos.
"""
import re
import time
from app.models.schemas import CKO
from app.utils.pubmed import pesquisar_pubmed
from app.utils.semantic_scholar import executar_busca as s2_busca
from app.utils.openalex import executar_busca as oa_busca
from app.utils.crossref import validar_lote as crossref_validar, buscar as crossref_buscar
from app.utils.europe_pmc import executar_busca as epmc_busca, enriquecer_com_pmc
from app.utils.unpaywall import enriquecer_pool as unpaywall_enriquecer
from app.services.llm_router import chamar, extrair_json, Complexidade

# ── Cache TTL em memória (único processo no Render) ───────────────────────────
# Evita refazer 7 chamadas externas para o mesmo diagnóstico+intervenção.
# TTL de 6 h — literatura médica não muda a ponto de invalidar em horas.
_BIBLIO_CACHE: dict[str, tuple[float, list[dict]]] = {}
_BIBLIO_TTL   = 6 * 3600   # segundos
_BIBLIO_MAX   = 128         # entradas máximas — evita crescimento ilimitado


def _cache_key(cko: CKO) -> str:
    """Normaliza diagnóstico + tipo de intervenção como chave de cache."""
    diag = re.sub(r"\s+", " ", (cko.diagnostico.diagnostico_definitivo or "").lower().strip())
    inter = re.sub(r"\s+", " ", (cko.intervencao.tipo or "").lower().strip())
    return f"{diag}::{inter}"


def _cache_get(key: str) -> list[dict] | None:
    entry = _BIBLIO_CACHE.get(key)
    if entry and time.time() - entry[0] < _BIBLIO_TTL:
        return entry[1]
    _BIBLIO_CACHE.pop(key, None)
    return None


def _cache_set(key: str, artigos: list[dict]) -> None:
    if len(_BIBLIO_CACHE) >= _BIBLIO_MAX:
        # Remove a entrada mais antiga
        oldest = min(_BIBLIO_CACHE, key=lambda k: _BIBLIO_CACHE[k][0])
        _BIBLIO_CACHE.pop(oldest, None)
    _BIBLIO_CACHE[key] = (time.time(), artigos)


_SYSTEM = """Você é um especialista em busca bibliográfica biomédica.
Construa queries PubMed otimizadas. Responda APENAS com JSON válido."""

_TEMPLATE = """Dado o caso clínico abaixo, gere exatamente 5 queries PubMed em inglês.

DIAGNÓSTICO: {diagnostico}
INTERVENÇÃO: {intervencao}
PROBLEMA CLÍNICO: {problema}
ÁREA: {area}

REGRAS:
1. Query 1: diagnóstico + "case report"[Publication Type] (últimos 10 anos)
2. Query 2: diagnóstico + intervenção (últimos 5 anos, sem case reports)
3. Query 3: fisiopatologia/epidemiologia — Reviews/Systematic Reviews (últimos 10 anos)
4. Query 4: diagnósticos diferenciais
5. Query 5: termos MeSH amplos do diagnóstico + tratamento de primeira linha

Responda com JSON:
{{"queries": ["q1", "q2", "q3", "q4", "q5"]}}"""


async def _construir_queries(cko: CKO) -> list[str]:
    prompt = _TEMPLATE.format(
        diagnostico=cko.diagnostico.diagnostico_definitivo,
        intervencao=f"{cko.intervencao.tipo}: {cko.intervencao.descricao[:150]}",
        problema=cko.editorial.problemas_clinicos[:250],
        area=f"{cko.editorial.area_atuacao or ''} {cko.editorial.especialidade or ''}".strip(),
    )
    try:
        resp = await chamar(_SYSTEM, prompt, complexidade=Complexidade.MEDIA,
                            max_tokens=512, json_mode=True)
        return extrair_json(resp).get("queries", [])
    except Exception:
        diag = cko.diagnostico.diagnostico_definitivo
        return [
            f'("{diag}"[Title/Abstract]) AND "case report"[Publication Type]',
            f'("{diag}"[Title/Abstract]) AND (treatment OR therapy)',
            f'("{diag}"[MeSH Terms]) AND (review[pt] OR systematic review[pt])',
            f'("{diag}"[Title/Abstract]) AND (diagnosis OR differential)',
            f'("{diag}"[MeSH Terms])',
        ]


def _vancouver(art: dict) -> str:
    ref = f"{art.get('autores', '')}. {art.get('titulo', '').rstrip('.')}. "
    ref += f"{art.get('periodico_abrev') or art.get('periodico', '')}. {art.get('ano', '')}"
    if art.get("volume"):
        ref += f";{art['volume']}"
    if art.get("numero"):
        ref += f"({art['numero']})"
    if art.get("paginas"):
        ref += f":{art['paginas']}"
    ref += "."
    if art.get("doi"):
        ref += f" doi: {art['doi']}"
    return ref


def _deduplicar(artigos: list[dict]) -> list[dict]:
    vistos_doi, vistos_pmid, unicos = set(), set(), []
    for art in artigos:
        doi  = art.get("doi", "").strip()
        pmid = art.get("pmid", "").strip()
        if (doi and doi in vistos_doi) or (pmid and pmid in vistos_pmid):
            continue
        if doi:
            vistos_doi.add(doi)
        if pmid:
            vistos_pmid.add(pmid)
        unicos.append(art)
    return unicos


async def executar(cko: CKO) -> list[dict]:
    # ── Cache hit — economiza 7 chamadas externas e ~30 s de latência ─────────
    key = _cache_key(cko)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    queries = await _construir_queries(cko)
    query_principal = f"{cko.diagnostico.diagnostico_definitivo} {cko.intervencao.tipo}"
    diag = cko.diagnostico.diagnostico_definitivo

    # ── 1. PubMed — busca ampliada (15 por query, até 40 no total) ───────────
    artigos_pubmed = await pesquisar_pubmed(queries, max_por_query=15, max_total=40)

    # Fallback: se PubMed retornou pouco, adiciona queries mais amplas
    if len(artigos_pubmed) < 12:
        fallback_queries = [
            f'("{diag}"[Title/Abstract]) AND (surgery OR treatment OR therapy)',
            f'("{diag}"[MeSH Terms])',
            f'{diag} management',
        ]
        artigos_extra = await pesquisar_pubmed(fallback_queries, max_por_query=12, max_total=20)
        artigos_pubmed = _deduplicar(artigos_pubmed + artigos_extra)

    # ── 2. Semantic Scholar ──────────────────────────────────────────────────
    pmids = [a["pmid"] for a in artigos_pubmed if a.get("pmid")]
    artigos_s2, enrichment_s2 = await s2_busca(query_principal, pmids)

    # Aplicar enriquecimento S2 nos artigos PubMed
    for art in artigos_pubmed:
        pmid = art.get("pmid", "")
        if pmid in enrichment_s2:
            art["citation_count"] = enrichment_s2[pmid].get("citation_count", 0)
            if not art.get("open_access_url"):
                art["open_access_url"] = enrichment_s2[pmid].get("open_access_url", "")

    # ── 3. OpenAlex ──────────────────────────────────────────────────────────
    artigos_oa = await oa_busca(query_principal, diag)

    # ── 4. Crossref — busca direta como fonte adicional ───────────────────────
    try:
        artigos_crossref = await crossref_buscar(diag, max_results=15)
    except Exception:
        artigos_crossref = []

    # ── 5. Europe PMC — cobertura OA adicional ───────────────────────────────
    try:
        artigos_epmc = await epmc_busca(query_principal, diag, ano_min=2010)
    except Exception:
        artigos_epmc = []

    # ── Mesclar todas as fontes ───────────────────────────────────────────────
    pool = _deduplicar(artigos_pubmed + artigos_s2 + artigos_oa + artigos_crossref + artigos_epmc)

    # ── 6. Crossref — validar DOIs (anti-alucinação): remove APENAS 404 ──────
    dois = [a["doi"] for a in pool if a.get("doi")]
    if dois:
        validacoes = await crossref_validar(dois)
        # Remove only articles whose DOI is DEFINITIVELY not found (HTTP 404)
        pool = [
            art for art in pool
            if not art.get("doi") or validacoes.get(art["doi"], {}).get("valido", True)
        ]

    # ── 7a. Europe PMC — enriquecer PMCIDs sem open_access_url ───────────────
    pool = await enriquecer_com_pmc(pool)

    # ── 7b. Unpaywall — adicionar PDF open access ─────────────────────────────
    pool = await unpaywall_enriquecer(pool)

    # ── 8. Semantic Ranking — re-rankeia por relevância semântica ao caso ─────
    # Não-bloqueante: falha silenciosa mantém ordenação por citações.
    try:
        from app.services.semantic_rank import ranquear
        contexto_rank = (
            f"Diagnóstico: {cko.diagnostico.diagnostico_definitivo}. "
            f"Intervenção: {cko.intervencao.tipo} — {cko.intervencao.descricao[:120]}. "
            f"Problema clínico: {cko.editorial.problemas_clinicos[:200]}."
        )
        pool = await ranquear(pool[:20], contexto_rank) + pool[20:]
    except Exception as _rank_err:
        import logging as _log
        _log.getLogger(__name__).warning("Semantic ranking ignorado: %s", _rank_err)

    # ── Ordenar e numerar (máx 40 para dar margem ao redator escolher ≥15) ────
    # Prioriza semantico_top, depois relevancia_semantica, depois citações.
    pool = sorted(
        pool,
        key=lambda x: (
            -int(x.get("semantico_top") or 0),
            -(x.get("relevancia_semantica") or 0),
            -(x.get("citation_count") or 0),
        ),
    )[:40]

    for i, art in enumerate(pool, start=1):
        art["numero"] = i
        art["formatada"] = _vancouver(art)

    _cache_set(key, pool)
    return pool
