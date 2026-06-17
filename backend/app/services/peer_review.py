"""
Peer Review Mode — Claude Sonnet como editor de revista científica internacional.

Recebe o artigo gerado + CKO e devolve uma revisão crítica formal em inglês
no estilo "Major/Minor Revisions" de um periódico biomédico.

Estrutura de retorno:
  {
    "recommendation": "Accept" | "Minor Revision" | "Major Revision" | "Reject",
    "summary":        str,          # parágrafo de síntese em inglês
    "strengths":      [str, ...],   # pontos fortes (max 4)
    "major_issues":   [str, ...],   # problemas que impedem publicação (max 6)
    "minor_issues":   [str, ...],   # melhorias não-bloqueantes (max 6)
    "care_compliance":str,          # análise de conformidade CARE 2013
  }
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a senior editor and peer reviewer for an international medical journal \
that publishes case reports (e.g. BMJ Case Reports, JAMA Case Reports, \
Journal of Medical Case Reports). You are reviewing a case report submitted \
by a Brazilian clinician, already structured according to CARE 2013 guidelines.

Write a formal peer review in English. Be rigorous but constructive.
Your tone should mirror a real reviewer's email — direct, professional, \
no unnecessary padding.

Respond ONLY with valid JSON (no markdown, no explanation outside the JSON):
{
  "recommendation": "Accept" | "Minor Revision" | "Major Revision" | "Reject",
  "summary": "One paragraph summarizing your overall assessment...",
  "strengths": ["strength 1", "strength 2"],
  "major_issues": ["issue 1", "issue 2"],
  "minor_issues": ["issue 1", "issue 2"],
  "care_compliance": "Assessment of CARE 2013 adherence..."
}\
"""

_TEMPLATE = """\
Please review the following case report manuscript.

TITLE: {titulo}

CARE SCORE: {care_score}/26 items satisfied

KEYWORDS: {keywords}

ABSTRACT:
{abstract}

INTRODUCTION (first 300 chars):
{introducao}

CASE REPORT (first 400 chars):
{caso_clinico}

DISCUSSION (first 400 chars):
{discussao}

CONCLUSION (first 200 chars):
{conclusao}

REFERENCES: {n_refs} references cited

PATIENT CONTEXT (anonymized):
- Demographics: {demograficos}
- Chief complaint: {queixa}
- Diagnosis: {diagnostico}
- Intervention: {intervencao}
- Outcome: {desfecho}

Based on the above, provide your structured peer review.\
"""


def _primeiros(lista_ou_str: Any, chars: int = 300) -> str:
    """Extrai os primeiros N chars de uma seção (lista de parágrafos ou string)."""
    if isinstance(lista_ou_str, list):
        texto = " ".join(str(p) for p in lista_ou_str if p)
    else:
        texto = str(lista_ou_str or "")
    return texto[:chars].strip() or "—"


def _construir_prompt(resultado: dict, cko: dict, care_score: int) -> str:
    res = resultado or {}
    resumo = res.get("resumo") or {}
    abstract = " ".join(filter(None, [
        resumo.get("introducao"), resumo.get("caso") or resumo.get("relato"),
        resumo.get("discussao"), resumo.get("conclusao"),
    ]))

    ident   = cko.get("identificacao") or {}
    hist    = cko.get("historia") or {}
    diag    = cko.get("diagnostico") or {}
    interv  = cko.get("intervencao") or {}
    desfech = cko.get("desfechos") or {}
    edit    = cko.get("editorial") or {}

    keywords = ", ".join(res.get("palavras_chave") or []) or "—"
    demograficos = (
        f"Age {ident.get('idade', '?')} {ident.get('idade_unidade','')}, "
        f"sex {ident.get('sexo','?')}"
    ).strip(", ")

    return _TEMPLATE.format(
        titulo       = str(res.get("titulo") or "Untitled Case Report")[:120],
        care_score   = care_score,
        keywords     = keywords[:200],
        abstract     = abstract[:500] or "—",
        introducao   = _primeiros(res.get("introducao"), 300),
        caso_clinico = _primeiros(res.get("caso_clinico"), 400),
        discussao    = _primeiros(res.get("discussao"), 400),
        conclusao    = _primeiros(res.get("conclusao"), 200),
        n_refs       = len(res.get("referencias") or []),
        demograficos = demograficos,
        queixa       = str(hist.get("queixa_principal") or "—")[:100],
        diagnostico  = str(diag.get("diagnostico_definitivo") or "—")[:100],
        intervencao  = f"{interv.get('tipo','')} — {str(interv.get('descricao',''))[:80]}".strip(" —"),
        desfecho     = str(desfech.get("desfecho_clinico") or "—")[:100],
    )


async def gerar(resultado: dict, cko: dict, care_score: int = 0) -> dict:
    """
    Gera a revisão por pares via Claude Sonnet 4.6 (ALTA).
    Raises RuntimeError se todos os providers falharem.
    """
    from app.services.llm_router import chamar, extrair_json, Complexidade

    prompt = _construir_prompt(resultado, cko, care_score)

    resp = await chamar(
        _SYSTEM, prompt,
        complexidade=Complexidade.ALTA,
        max_tokens=1200,
        json_mode=True,
    )

    try:
        data = extrair_json(resp)
    except Exception as exc:
        logger.error("Peer review: falha ao parsear JSON do LLM: %s — raw: %s", exc, resp[:300])
        raise

    # Garante campos obrigatórios com fallbacks
    validas = {"Accept", "Minor Revision", "Major Revision", "Reject"}
    return {
        "recommendation": data.get("recommendation", "Major Revision")
                          if data.get("recommendation") in validas else "Major Revision",
        "summary":        str(data.get("summary") or ""),
        "strengths":      [str(s) for s in (data.get("strengths") or [])][:4],
        "major_issues":   [str(s) for s in (data.get("major_issues") or [])][:6],
        "minor_issues":   [str(s) for s in (data.get("minor_issues") or [])][:6],
        "care_compliance":str(data.get("care_compliance") or ""),
    }
