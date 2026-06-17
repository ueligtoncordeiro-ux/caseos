"""
CARE Live Score — avaliação algorítmica em tempo real (sem LLM).

Mapeia cada um dos 26 itens do checklist CARE 2013 para campos do formulário.
Retorna score, itens atendidos/faltantes e sugestão priorizada por etapa.

Usado pelo endpoint POST /artigo/care-preview para feedback instantâneo
enquanto o usuário preenche o formulário (debounce ~800ms no front).

A função `sugestao_ia()` (separada) chama Gemini Flash com o snapshot
dos campos preenchidos e retorna uma sugestão qualitativa personalizada.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


# ── Mapeamento CARE 2013 → etapa do formulário ────────────────────────────────
# etapa: número da etapa (1-10) no formulário novo-relato.html
# label: texto curto para o painel
# label_longo: explicação completa para o tooltip

@dataclass
class ItemCARE:
    id: str
    etapa: int
    label: str
    label_longo: str
    peso: int = 1          # peso futuro; hoje todos valem 1


_ITENS: list[ItemCARE] = [
    # Etapa 10 — Editorial (care_1: título)
    ItemCARE("care_1",  10, "Diagnóstico no título",       "Título deve conter o diagnóstico e 'relato de caso'"),
    # Etapa 10 — serão geradas as keywords
    ItemCARE("care_2",  10, "Palavras-chave",              "2-5 descritores MeSH cobrindo diagnóstico e intervenção"),
    # Gerado automaticamente
    ItemCARE("care_3",  10, "Resumo estruturado",          "Resumo com seções: Introdução · Caso · Discussão · Conclusão"),
    # Etapa 10 — Editorial
    ItemCARE("care_4",  10, "Introdução e objetivo",       "Contexto epidemiológico do diagnóstico e objetivo do relato"),
    # Etapa 1 — Identificação
    ItemCARE("care_5a",  1, "Dados demográficos",          "Idade (número), sexo biológico e procedência do paciente"),
    # Etapa 2 — História Clínica
    ItemCARE("care_5b",  2, "Queixa principal",            "Queixa clara com tempo de evolução dos sintomas"),
    # Etapa 2 — História Clínica
    ItemCARE("care_5c",  2, "História clínica completa",   "HDA detalhada + antecedentes pessoais e familiares relevantes"),
    # Etapa 3 — Intervenções Anteriores
    ItemCARE("care_5d",  3, "Intervenções anteriores",     "Tratamentos, medicamentos e alergias prévias documentados"),
    # Etapa 4 — Achados Clínicos
    ItemCARE("care_6",   4, "Exame físico documentado",    "Achados gerais e específicos ao exame, com sinais vitais"),
    # Etapa 5 — Linha do Tempo
    ItemCARE("care_7",   5, "Linha do tempo (≥3 eventos)", "Cronologia com pelo menos 3 marcos temporais do caso"),
    # Etapa 6 — Diagnóstico
    ItemCARE("care_8a",  6, "Métodos diagnósticos",        "Exames laboratoriais e/ou de imagem que embasaram o diagnóstico"),
    # Etapa 6 — Diagnóstico
    ItemCARE("care_8b",  6, "Desafios diagnósticos",       "Dificuldades encontradas no processo diagnóstico"),
    # Etapa 6 — Diagnóstico
    ItemCARE("care_8c",  6, "Diagnóstico definitivo",      "Diagnóstico final confirmado com critérios utilizados"),
    # Etapa 6 — Diagnóstico
    ItemCARE("care_8d",  6, "Prognóstico documentado",     "Expectativa de evolução e fatores prognósticos"),
    # Etapa 7 — Intervenção
    ItemCARE("care_9a",  7, "Tipo de intervenção",         "Tipo de tratamento realizado (cirúrgico, farmacológico, etc.)"),
    # Etapa 7 — Intervenção
    ItemCARE("care_9b",  7, "Detalhes da intervenção",     "Descrição detalhada: dose, duração, técnica, profissional"),
    # Etapa 7 — Intervenção
    ItemCARE("care_9c",  7, "Mudanças na intervenção",     "Alterações no tratamento durante o caso, com justificativa"),
    # Etapa 8 — Desfechos
    ItemCARE("care_10a", 8, "Desfecho clínico",            "Resultado final observado pelo clínico com detalhes objetivos"),
    # Etapa 8 — Desfechos
    ItemCARE("care_10b", 8, "Exames de seguimento",        "Resultados de exames pós-tratamento ou no follow-up"),
    # Etapa 8 — Desfechos
    ItemCARE("care_10c", 8, "Adesão ao tratamento",        "Informação sobre adesão e tolerabilidade do paciente"),
    # Etapa 8 — Desfechos
    ItemCARE("care_10d", 8, "Eventos adversos",            "Efeitos adversos ou complicações (documente mesmo que ausentes)"),
    # Etapa 10 — Editorial
    ItemCARE("care_11a", 10, "Limitações do relato",       "Limitações metodológicas e possíveis vieses documentados"),
    # Etapa 10 — Editorial
    ItemCARE("care_11b", 10, "Discussão com literatura",   "Comparação com casos similares publicados"),
    # Etapa 10 — Editorial
    ItemCARE("care_11c", 10, "Racional das conclusões",    "Justificativa baseada em evidências para as conclusões"),
    # Etapa 10 — Editorial
    ItemCARE("care_11d", 10, "Lições principais",          "Take-away messages claros para o leitor clínico"),
    # Etapa 9 — Perspectiva
    ItemCARE("care_12",  9, "Perspectiva do paciente",     "Relato do paciente sobre sua experiência e perspectiva"),
    # Etapa 1 — Consentimento
    ItemCARE("care_13",  1, "Consentimento documentado",   "Declaração de consentimento informado do paciente"),
]

TOTAL_ITENS = len(_ITENS)   # 26
_ITEM_MAP   = {i.id: i for i in _ITENS}


def _preenchido(valor, min_len: int = 1) -> bool:
    """True se valor é string não-vazia com pelo menos min_len chars."""
    if valor is None:
        return False
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return True
    return len(str(valor).strip()) >= min_len


def _score_item(item_id: str, dados: dict) -> bool:
    """
    Retorna True se o item CARE está satisfeito com base nos dados do formulário.
    `dados` é um dict flat com os campos do CKO parcial.
    """
    d = dados  # alias curto

    match item_id:
        # ── Etapa 1 ────────────────────────────────────────────────────────
        case "care_5a":
            return _preenchido(d.get("idade"), 1) and _preenchido(d.get("sexo"), 2)
        case "care_13":
            return bool(d.get("decl_responsavel")) or bool(d.get("consentimento_editorial"))

        # ── Etapa 2 ────────────────────────────────────────────────────────
        case "care_5b":
            return _preenchido(d.get("queixa_principal"), 10) and _preenchido(d.get("duracao_sintomas"), 3)
        case "care_5c":
            return _preenchido(d.get("hda"), 80)

        # ── Etapa 3 ────────────────────────────────────────────────────────
        case "care_5d":
            return (
                _preenchido(d.get("tratamentos"), 5)
                or _preenchido(d.get("medicamentos"), 5)
                or _preenchido(d.get("alergias"), 3)
            )

        # ── Etapa 4 ────────────────────────────────────────────────────────
        case "care_6":
            return _preenchido(d.get("exame_geral"), 40) and _preenchido(d.get("achados_especificos"), 30)

        # ── Etapa 5 ────────────────────────────────────────────────────────
        case "care_7":
            eventos = d.get("eventos_timeline", [])
            return isinstance(eventos, list) and len(eventos) >= 3

        # ── Etapa 6 ────────────────────────────────────────────────────────
        case "care_8a":
            return _preenchido(d.get("exames_lab"), 10) or _preenchido(d.get("exames_imagem"), 10)
        case "care_8b":
            return _preenchido(d.get("desafios"), 20)
        case "care_8c":
            return _preenchido(d.get("diagnostico_definitivo"), 5)
        case "care_8d":
            return _preenchido(d.get("prognostico"), 10)

        # ── Etapa 7 ────────────────────────────────────────────────────────
        case "care_9a":
            return _preenchido(d.get("intervencao_tipo"), 5)
        case "care_9b":
            return _preenchido(d.get("intervencao_descricao"), 80)
        case "care_9c":
            houve = d.get("houve_mudanca", False)
            if not houve:
                return True   # sem mudança também é informação documentada
            return _preenchido(d.get("desc_mudanca"), 20)

        # ── Etapa 8 ────────────────────────────────────────────────────────
        case "care_10a":
            return _preenchido(d.get("desfecho_clinico"), 40)
        case "care_10b":
            return _preenchido(d.get("exames_seguimento"), 20)
        case "care_10c":
            return _preenchido(d.get("adesao"), 5)
        case "care_10d":
            return _preenchido(d.get("eventos_adversos"), 3)

        # ── Etapa 9 ────────────────────────────────────────────────────────
        case "care_12":
            return _preenchido(d.get("perspectiva_paciente"), 30)

        # ── Etapa 10 ───────────────────────────────────────────────────────
        case "care_1":
            return _preenchido(d.get("diagnostico_definitivo"), 5)
        case "care_2":
            # Será gerado — satisfeito se diagnóstico + intervenção preenchidos
            return (
                _preenchido(d.get("diagnostico_definitivo"), 5)
                and _preenchido(d.get("intervencao_tipo"), 3)
            )
        case "care_3":
            # Resumo será gerado — satisfeito se houver dados suficientes para gerá-lo
            campos_core = [
                d.get("diagnostico_definitivo"), d.get("hda"),
                d.get("intervencao_tipo"), d.get("desfecho_clinico"),
            ]
            return sum(1 for c in campos_core if _preenchido(c, 10)) >= 3
        case "care_4":
            return _preenchido(d.get("problemas_clinicos"), 80)
        case "care_11a":
            # Limitações emergem da discussão — satisfeito se editorial tiver contexto suficiente
            return _preenchido(d.get("diferencial_caso"), 60)
        case "care_11b":
            return _preenchido(d.get("problemas_clinicos"), 100)
        case "care_11c":
            return _preenchido(d.get("diferencial_caso"), 80)
        case "care_11d":
            return _preenchido(d.get("problemas_clinicos"), 150)

    return False


# ── Labels de etapa para o painel ─────────────────────────────────────────────

_ETAPA_LABEL = {
    1: "Identificação",
    2: "História Clínica",
    3: "Intervenções Ant.",
    4: "Achados Clínicos",
    5: "Linha do Tempo",
    6: "Diagnóstico",
    7: "Intervenção",
    8: "Desfechos",
    9: "Perspectiva",
    10: "Editorial",
}


def calcular(dados: dict) -> dict:
    """
    Avaliação algorítmica pura — sem LLM.
    Retorna em < 5ms.

    Returns:
        {
          "score": int,          # 0-26
          "max": 26,
          "pct": int,            # 0-100
          "atendidos": [str],    # IDs dos itens satisfeitos
          "faltantes": [         # itens pendentes, ordenados por etapa
            { "id", "etapa", "etapa_label", "label", "label_longo" }
          ],
          "por_etapa": {         # score por etapa (para mini-barras)
            "1": {"score": 2, "max": 2},
            ...
          },
          "proxima_etapa_fraca": int | None,  # etapa com mais itens faltantes
          "sugestao_rapida": str,             # dica imediata sem LLM
        }
    """
    atendidos: list[str] = []
    faltantes: list[dict] = []

    por_etapa: dict[int, dict] = {e: {"score": 0, "max": 0} for e in range(1, 11)}

    for item in _ITENS:
        por_etapa[item.etapa]["max"] += 1
        ok = _score_item(item.id, dados)
        if ok:
            atendidos.append(item.id)
            por_etapa[item.etapa]["score"] += 1
        else:
            faltantes.append({
                "id":          item.id,
                "etapa":       item.etapa,
                "etapa_label": _ETAPA_LABEL.get(item.etapa, f"Etapa {item.etapa}"),
                "label":       item.label,
                "label_longo": item.label_longo,
            })

    score = len(atendidos)
    pct   = round(score / TOTAL_ITENS * 100)

    # Etapa com mais itens faltantes (para seta de foco)
    contagem_falta: dict[int, int] = {}
    for f in faltantes:
        contagem_falta[f["etapa"]] = contagem_falta.get(f["etapa"], 0) + 1
    proxima_fraca = max(contagem_falta, key=lambda e: contagem_falta[e], default=None) if contagem_falta else None

    # Sugestão rápida algorítmica (o mais impactante sem LLM)
    sugestao = _sugestao_rapida(faltantes, dados, proxima_fraca)

    return {
        "score":               score,
        "max":                 TOTAL_ITENS,
        "pct":                 pct,
        "atendidos":           atendidos,
        "faltantes":           faltantes[:8],   # top 8 para o painel
        "por_etapa":           {str(k): v for k, v in por_etapa.items()},
        "proxima_etapa_fraca": proxima_fraca,
        "sugestao_rapida":     sugestao,
    }


def _sugestao_rapida(faltantes: list[dict], dados: dict, etapa_fraca: Optional[int]) -> str:
    """Gera uma dica textual instantânea baseada nos itens faltantes."""
    if not faltantes:
        return "✅ Formulário completo! Pronto para gerar o artigo."

    # Prioriza itens de alto impacto
    prioridade = ["care_7", "care_12", "care_5c", "care_6", "care_8b", "care_4", "care_10a"]
    for p in prioridade:
        for f in faltantes:
            if f["id"] == p:
                return f"💡 {f['label_longo']}"

    # Fallback: primeiro item faltante
    return f"💡 {faltantes[0]['label_longo']}"


# ── Sugestão via IA (Gemini Flash) ────────────────────────────────────────────

_SYS_SUGESTAO = """Você é um especialista em CARE 2013 e relatos de caso clínico.
Analise os campos preenchidos e retorne UMA sugestão objetiva e específica
para aumentar o CARE Score. Máximo 2 frases. Português brasileiro. Seja cirúrgico.
Responda apenas JSON: {"sugestao": "...", "campo_foco": "nome do campo"}"""


async def sugestao_ia(dados: dict, score_atual: int, faltantes: list[dict]) -> dict:
    """
    Sugestão qualitativa via Gemini Flash.
    Chamada separada — só disparada quando usuário clica em 'Analisar'.

    Returns: { "sugestao": str, "campo_foco": str }
    """
    from app.services.llm_router import chamar, extrair_json, Complexidade

    falt_txt = "\n".join(f"- {f['label']}: {f['label_longo']}" for f in faltantes[:6])

    # Resumo do que está preenchido
    preenchidos = []
    if dados.get("diagnostico_definitivo"):
        preenchidos.append(f"Diagnóstico: {str(dados['diagnostico_definitivo'])[:60]}")
    if dados.get("intervencao_tipo"):
        preenchidos.append(f"Intervenção: {str(dados['intervencao_tipo'])[:60]}")
    if dados.get("hda"):
        preenchidos.append(f"HDA: {str(dados['hda'])[:80]}...")
    if dados.get("desfecho_clinico"):
        preenchidos.append(f"Desfecho: {str(dados['desfecho_clinico'])[:60]}")

    prompt = f"""CARE Score atual: {score_atual}/26

CAMPOS PREENCHIDOS:
{chr(10).join(preenchidos) if preenchidos else "Poucos campos preenchidos."}

ITENS CARE FALTANTES:
{falt_txt if falt_txt else "Nenhum."}

Dê UMA sugestão específica e acionável para o campo mais impactante faltante."""

    try:
        resp = await chamar(
            _SYS_SUGESTAO, prompt,
            complexidade=Complexidade.BAIXA,
            max_tokens=150,
            json_mode=True,
        )
        data = extrair_json(resp)
        return {
            "sugestao":    data.get("sugestao", ""),
            "campo_foco":  data.get("campo_foco", ""),
        }
    except Exception as exc:
        # Fallback para sugestão algorítmica em caso de falha do LLM
        return {
            "sugestao":   _sugestao_rapida(faltantes, dados, None),
            "campo_foco": faltantes[0]["label"] if faltantes else "",
            "_fallback":  str(exc)[:100],
        }
