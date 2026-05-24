"""
Agente Editor — melhoria de seção individual do artigo.
Consome ~3.000 tokens (vs ~19.000 da geração completa).
"""
from app.services.llm_router import chamar, extrair_json, Complexidade

_SECOES_NOMES = {
    "introducao": "Introdução",
    "caso_clinico": "Relato do Caso",
    "discussao": "Discussão",
    "conclusao": "Conclusão",
}

_SYSTEM = """Você é um redator científico sênior especialista em relatos de caso clínico.
Reescreva APENAS a seção solicitada, mantendo:
- Português brasileiro formal e técnico
- Terceira pessoa impessoal ou voz passiva (NUNCA primeira pessoa do plural)
- Citações [N] existentes preservadas no texto
- Travessão (—) PROIBIDO — use vírgula, ponto e vírgula ou dois-pontos
- Palavras proibidas: inequivocamente, corrobora, amplamente, robusto, notável, fundamental,
  crucial, imperativo, ressalta, evidencia, destaca-se, cabe ressaltar, vale destacar
Responda APENAS com JSON válido."""

async def melhorar_secao_llm(
    secao: str,
    conteudo_atual: str,
    instrucao: str,
    cko: dict,
) -> list[str]:
    nome_secao = _SECOES_NOMES.get(secao, secao)
    diag = (cko.get("diagnostico") or {}).get("diagnostico_definitivo", "não especificado")

    instrucao_extra = f"\nINSTRUÇÃO ADICIONAL DO USUÁRIO: {instrucao}" if instrucao.strip() else ""

    prompt = f"""Melhore a seção "{nome_secao}" do relato de caso clínico abaixo.
Diagnóstico do caso: {diag}

TEXTO ATUAL:
{conteudo_atual}
{instrucao_extra}

REGRAS:
- Mantenha o mesmo número de parágrafos (ou reduza se houver repetição)
- Não invente dados clínicos — use apenas os que estão no texto atual
- Cada parágrafo deve ser autossuficiente e bem construído
- Melhore clareza, fluidez e precisão científica
- Preserve as citações [N] nos locais corretos

Responda com JSON:
{{"paragrafos": ["parágrafo 1 melhorado", "parágrafo 2 melhorado", ...]}}"""

    try:
        resp = await chamar(_SYSTEM, prompt, complexidade=Complexidade.MEDIA, max_tokens=3000, json_mode=True)
        data = extrair_json(resp)
        paragrafos = data.get("paragrafos", [])
        if paragrafos and isinstance(paragrafos, list):
            return [str(p) for p in paragrafos if p]
    except Exception:
        pass

    # Fallback: return original split by double newline
    return [p.strip() for p in conteudo_atual.split("\n\n") if p.strip()]
