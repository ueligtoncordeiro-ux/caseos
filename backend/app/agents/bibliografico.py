"""
Agente Bibliográfico — pipeline 5 fontes:

  1. GPT-4o mini constrói queries otimizadas
  2. PubMed      → base primária (35M artigos biomédicos)
  3. Semantic S2 → enriquece PMIDs + busca complementar (200M artigos)
  4. OpenAlex    → busca complementar (250M artigos, melhor cobertura BR)
  5. Crossref    → valida todos os DOIs (anti-alucinação)
  6. Unpaywall   → adiciona PDF open access ao pool

Pool final: deduplicado por DOI/PMID, ordenado por citações, máx 25 artigos.
"""
from app.models.schemas import CKO
from app.utils.pubmed import pesquisar_pubmed
from app.utils.semantic_scholar import executar_busca as s2_busca
from app.utils.openalex import executar_busca as oa_busca
from app.utils.crossref import validar_lote as crossref_validar
from app.utils.unpaywall import enriquecer_pool as unpaywall_enriquecer
from app.services.llm_router import chamar, extrair_json, Complexidade

_SYSTEM = """Você é um especialista em busca bibliográfica biomédica.
Construa queries PubMed otimizadas. Responda APENAS com JSON válido."""

_TEMPLATE = """Dado o caso clínico abaixo, gere exatamente 4 queries PubMed em inglês.

DIAGNÓSTICO: {diagnostico}
INTERVENÇÃO: {intervencao}
PROBLEMA CLÍNICO: {problema}
ÁREA: {area}

REGRAS:
1. Query 1: diagnóstico + "case report"[Publication Type] (últimos 10 anos)
2. Query 2: diagnóstico + intervenção (últimos 5 anos, sem case reports)
3. Query 3: fisiopatologia/epidemiologia — Reviews/Systematic Reviews (últimos 10 anos)
4. Query 4: diagnósticos diferenciais

Responda com JSON:
{{"queries": ["q1", "q2", "q3", "q4"]}}"""


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
    queries = await _construir_queries(cko)
    query_principal = f"{cko.diagnostico.diagnostico_definitivo} {cko.intervencao.tipo}"

    # ── 1. PubMed — busca ampliada (12 por query, até 30 no total) ───────────
    artigos_pubmed = await pesquisar_pubmed(queries, max_por_query=12, max_total=30)

    # Fallback: se PubMed retornou pouco, adiciona query genérica mais ampla
    if len(artigos_pubmed) < 10:
        diag = cko.diagnostico.diagnostico_definitivo
        fallback_queries = [
            f'("{diag}"[Title/Abstract]) AND (surgery OR treatment OR therapy)',
            f'("{diag}"[MeSH Terms])',
        ]
        artigos_extra = await pesquisar_pubmed(fallback_queries, max_por_query=10, max_total=15)
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
    artigos_oa = await oa_busca(query_principal, cko.diagnostico.diagnostico_definitivo)

    # ── Mesclar todas as fontes ───────────────────────────────────────────────
    pool = _deduplicar(artigos_pubmed + artigos_s2 + artigos_oa)

    # ── 4. Crossref — validar DOIs (anti-alucinação) ──────────────────────────
    dois = [a["doi"] for a in pool if a.get("doi")]
    if dois:
        validacoes = await crossref_validar(dois)
        # Remover artigos com DOI inválido (não encontrado na Crossref)
        pool = [
            art for art in pool
            if not art.get("doi") or validacoes.get(art["doi"], {}).get("valido", True)
        ]

    # ── 5. Unpaywall — adicionar PDF open access ──────────────────────────────
    pool = await unpaywall_enriquecer(pool)

    # ── Ordenar e numerar (máx 30 para dar margem ao redator escolher ≥15) ────
    pool = sorted(pool, key=lambda x: x.get("citation_count", 0), reverse=True)[:30]

    for i, art in enumerate(pool, start=1):
        art["numero"] = i
        art["formatada"] = _vancouver(art)

    return pool
