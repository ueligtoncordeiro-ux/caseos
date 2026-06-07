"""
Agente Revisor — duas camadas:
  Nível A (seções):  GPT-4o mini — correção linguística, mais barato (paralelo)
  Nível B (global):  Claude Sonnet 4.6 — CARE Score, flags, revisão científica profunda
"""
import asyncio
from app.models.schemas import CKO, ArtigoGerado, Resumo, RelatorioGerado
from app.services.llm_router import chamar, extrair_json, Complexidade

_CARE_ITENS = {
    "care_1":  "Título contém 'relato de caso' e área de foco",
    "care_2":  "Palavras-chave (2–5 descritores)",
    "care_3":  "Resumo estruturado (Introdução|Caso|Discussão|Conclusão)",
    "care_4":  "Introdução com contexto e objetivo",
    "care_5a": "Informações demográficas do paciente",
    "care_5b": "Queixa principal e sintomas",
    "care_5c": "História clínica, familiar e psicossocial",
    "care_5d": "Intervenções anteriores e desfechos",
    "care_6":  "Achados ao exame físico",
    "care_7":  "Linha do tempo",
    "care_8a": "Métodos diagnósticos",
    "care_8b": "Desafios diagnósticos",
    "care_8c": "Diagnóstico definitivo com diferenciais",
    "care_8d": "Prognóstico",
    "care_9a": "Tipo de intervenção",
    "care_9b": "Administração da intervenção (dose/duração)",
    "care_9c": "Mudanças na intervenção com justificativa",
    "care_10a":"Desfechos avaliados pelo clínico",
    "care_10b":"Exames de seguimento",
    "care_10c":"Adesão e tolerabilidade",
    "care_10d":"Eventos adversos",
    "care_11a":"Limitações e vieses",
    "care_11b":"Discussão com literatura relevante",
    "care_11c":"Racional para as conclusões",
    "care_11d":"Lições principais (take-away lessons)",
    "care_12": "Perspectiva do paciente",
    "care_13": "Consentimento informado declarado",
}

# ── Nível A: revisão linguística por GPT-4o mini ─────────────────────────────

_SYS_A = """Você é um revisor linguístico de textos científicos médicos em português brasileiro.
Corrija erros gramaticais, ortográficos, coesão e fluência. NÃO altere dados clínicos.
Responda APENAS com JSON válido."""

_TPL_A = """Revise os parágrafos abaixo. Corrija apenas erros de língua, não altere conteúdo.

SEÇÃO: {secao}
PARÁGRAFOS:
{paragrafos}

JSON: {{"paragrafos_revisados": ["P1 corrigido", "P2 corrigido", ...]}}"""


async def _revisar_secao(secao: str, paragrafos: list[str]) -> list[str]:
    if not paragrafos:
        return paragrafos
    texto = "\n\n".join(f"[{i+1}] {p}" for i, p in enumerate(paragrafos))
    prompt = _TPL_A.format(secao=secao, paragrafos=texto)
    try:
        resp = await chamar(_SYS_A, prompt, complexidade=Complexidade.MEDIA,
                            max_tokens=4096, json_mode=True)
        data = extrair_json(resp)
        revisados = data.get("paragrafos_revisados", paragrafos)
        return revisados if len(revisados) == len(paragrafos) else paragrafos
    except Exception:
        return paragrafos


# ── Nível B: revisão global por Claude Sonnet ─────────────────────────────────

_SYS_B = """Você é um editor científico sênior especialista em relatos de caso clínico
e no checklist CARE 2013. Avalie o artigo completo e produza o relatório de qualidade.
Responda APENAS com JSON válido."""

_TPL_B = """Avalie o artigo científico abaixo contra o checklist CARE 2013.

TÍTULO: {titulo}
RESUMO: Introdução: {ri} | Caso: {rc} | Discussão: {rd} | Conclusão: {rco}
INTRODUÇÃO (resumo): {intro}
CASO CLÍNICO (resumo): {caso}
DISCUSSÃO (resumo): {disc}
CONCLUSÃO: {conc}
TOTAL REFERÊNCIAS: {nrefs}

CHECKLIST CARE 2013:
{care}

CONTEXTO:
- Consentimento obtido: {consentimento}
- Perspectiva do paciente fornecida: {perspectiva}

Avalie cada item CARE e identifique flags para revisão humana.

JSON:
{{
  "care_score": <inteiro: quantidade de itens CARE satisfeitos, de 0 a {n_total}>,
  "care_itens_atendidos": ["care_1","care_2"],
  "care_itens_faltantes": ["care_12"],
  "flags": ["Descrição do que o autor precisa verificar ou completar"],
  "observacoes": ["Observação editorial geral"]
}}"""


async def _avaliar_care(artigo: ArtigoGerado, cko: CKO) -> RelatorioGerado:
    care_txt = "\n".join(f"- {k}: {v}" for k, v in _CARE_ITENS.items())

    def _resumo(paragrafos: list[str], chars: int = 400) -> str:
        txt = " ".join(paragrafos)
        return txt[:chars] + "..." if len(txt) > chars else txt

    prompt = _TPL_B.format(
        titulo=artigo.titulo,
        ri=artigo.resumo.introducao[:200],
        rc=artigo.resumo.caso[:200],
        rd=artigo.resumo.discussao[:200],
        rco=artigo.resumo.conclusao[:150],
        intro=_resumo(artigo.introducao),
        caso=_resumo(artigo.caso_clinico),
        disc=_resumo(artigo.discussao, 600),
        conc=_resumo(artigo.conclusao),
        nrefs=len(artigo.referencias),
        care=care_txt,
        n_total=len(_CARE_ITENS),
        consentimento="Sim" if cko.editorial.consentimento else "Não declarado",
        perspectiva="Sim" if cko.perspectiva_paciente else "Não",
    )

    resp = await chamar(_SYS_B, prompt, complexidade=Complexidade.ALTA, max_tokens=4096)
    data = extrair_json(resp)

    _CARE_MAX = len(_CARE_ITENS)  # 26 itens no checklist CARE 2013

    return RelatorioGerado(
        # Clamp: LLM pode alucinar valor fora do range válido
        care_score=min(_CARE_MAX, max(0, int(data.get("care_score", 0)))),
        care_itens_atendidos=data.get("care_itens_atendidos", [])[:_CARE_MAX],
        care_itens_faltantes=data.get("care_itens_faltantes", [])[:_CARE_MAX],
        flags=data.get("flags", []),
        bases_consultadas=4,
        total_referencias=len(artigo.referencias),
        observacoes=data.get("observacoes", []),
    )


# ── Executor principal ────────────────────────────────────────────────────────

async def executar(cko: CKO, artigo: ArtigoGerado) -> tuple[ArtigoGerado, RelatorioGerado]:
    # Nível A: GPT-4o mini revisa as 4 seções EM PARALELO (asyncio.gather)
    # Antes eram 4 awaits sequenciais — adicionava 2-4 min desnecessários ao pipeline
    intro_rev, caso_rev, disc_rev, conc_rev = await asyncio.gather(
        _revisar_secao("Introdução",   artigo.introducao),
        _revisar_secao("Caso Clínico", artigo.caso_clinico),
        _revisar_secao("Discussão",    artigo.discussao),
        _revisar_secao("Conclusão",    artigo.conclusao),
    )

    artigo_revisado = ArtigoGerado(
        titulo=artigo.titulo,
        palavras_chave=artigo.palavras_chave,
        resumo=artigo.resumo,
        introducao=intro_rev,
        caso_clinico=caso_rev,
        discussao=disc_rev,
        conclusao=conc_rev,
        referencias=artigo.referencias,
    )

    # Nível B: Claude avalia CARE Score e flags
    relatorio = await _avaliar_care(artigo_revisado, cko)

    return artigo_revisado, relatorio
