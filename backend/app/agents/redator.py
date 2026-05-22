"""
Agente Redator — Claude Sonnet 4.6 (alta complexidade).
Escrita científica longa em PT-BR exige contexto extenso e raciocínio profundo.
"""
from app.models.schemas import CKO, ArtigoGerado, Resumo, Referencia
from app.services.llm_router import chamar, extrair_json, Complexidade

_SYSTEM = """Você é um redator científico especialista em relatos de caso clínico,
com domínio do checklist CARE 2013, normas Vancouver/ICMJE e escrita médica em português brasileiro.

REGRAS ABSOLUTAS:
1. Escreva APENAS em português brasileiro formal e técnico
2. Tempo verbal: passado para eventos do caso, presente para discussão
3. Voz: terceira pessoa ("o paciente", "os autores")
4. Citações: formato [N] após o ponto final da afirmação citada
5. NÃO invente dados clínicos — use APENAS o que está no CKO
6. NÃO cite artigos fora da lista fornecida
7. Cada parágrafo da Discussão cita pelo menos 1 referência
8. Responda APENAS com JSON válido"""

_TEMPLATE = """Redija um relato de caso clínico científico completo e publicável.

══ CKO ══
PACIENTE: {idade} {idade_unidade} | {sexo} | Procedência: {procedencia}
QUEIXA: {queixa} (duração: {duracao})
HDA: {hda}
ANTECEDENTES: {antecedentes}
HISTÓRIA FAMILIAR: {hist_familiar}
INTERVENÇÕES ANTERIORES: tratamentos={trat_prev} | medicamentos={medicamentos} | alergias={alergias}
EXAME FÍSICO: {exame_geral}
ACHADOS ESPECÍFICOS: {achados_esp}
SINAIS VITAIS: {sinais_vitais}
LINHA DO TEMPO: {timeline}
EXAMES LAB: {lab}
IMAGEM: {imagem}
DIAGNÓSTICO: {diagnostico}
DIFERENCIAIS: {diferenciais}
DESAFIOS: {desafios}
INTERVENÇÃO ({tipo_intervencao}): {desc_intervencao}
MUDANÇA NA INTERVENÇÃO: {mudanca}
DESFECHO: {desfecho} | Adesão: {adesao} | Tempo: {tempo_acomp}
EVENTOS ADVERSOS: {eventos_adv}
PERSPECTIVA PACIENTE: {perspectiva}
PROBLEMA CLÍNICO: {problema}
DIFERENCIAL DO CASO: {diferencial}
PERIÓDICO: {periodico} | FORMATO: {formato_ref}

══ REFERÊNCIAS DISPONÍVEIS ══
{referencias}

══ ESTRUTURA OBRIGATÓRIA ══
TÍTULO: contém "relato de caso" + diagnóstico/procedimento principal
PALAVRAS-CHAVE: 3–5 descritores MeSH/DeCS em português
RESUMO: máx 250 palavras — 4 subseções (Introdução | Apresentação do Caso | Discussão | Conclusão)
INTRODUÇÃO: 3 parágrafos ~300 palavras — P1=contexto(cite 2), P2=fisiopatologia(cite 2), P3=objetivo
CASO CLÍNICO: narrativa cronológica ~450 palavras
DISCUSSÃO: 10 parágrafos ~800 palavras — cada um cita ao menos 1 ref
  P1=diferencial do caso, P2=epidemiologia, P3=fisiopatologia, P4=achados vs literatura,
  P5=métodos diagnósticos, P6=diagnósticos diferenciais, P7=intervenção,
  P8=desfechos, P9=perspectiva/limitações, P10=lições clínicas
CONCLUSÃO: 1–2 parágrafos ~120 palavras

JSON de resposta:
{{
  "titulo": "...",
  "palavras_chave": ["..."],
  "resumo": {{"introducao":"...","caso":"...","discussao":"...","conclusao":"..."}},
  "introducao": ["P1","P2","P3"],
  "caso_clinico": ["P1","P2","..."],
  "discussao": ["P1","P2","P3","P4","P5","P6","P7","P8","P9","P10"],
  "conclusao": ["P1"],
  "referencias_usadas": [1,2,3]
}}"""


def _timeline(cko: CKO) -> str:
    evs = [e for e in cko.timeline.eventos if e.evento]
    return "\n".join(f"  {e.tempo}: {e.evento}" for e in evs) if evs else "Não informada."


def _refs_txt(artigos: list[dict]) -> str:
    if not artigos:
        return "Nenhuma — use conhecimento médico geral."
    linhas = []
    for a in artigos:
        linha = f"[{a['numero']}] {a['formatada']}"
        if a.get("abstract"):
            linha += f"\n    Abstract: {a['abstract'][:250]}..."
        linhas.append(linha)
    return "\n\n".join(linhas)


async def executar(cko: CKO, artigos: list[dict]) -> ArtigoGerado:
    i = cko.identificacao
    h = cko.historia
    a = cko.achados
    d = cko.diagnostico
    iv = cko.intervencao
    de = cko.desfechos
    ed = cko.editorial
    pr = cko.intervencoes_anteriores

    mudanca = "Não"
    if iv.houve_mudanca:
        mudanca = f"Sim — {iv.desc_mudanca or ''} (justificativa: {iv.just_mudanca or ''})"

    prompt = _TEMPLATE.format(
        idade=i.idade or "não informada", idade_unidade=i.idade_unidade,
        sexo=i.sexo or "não informado", procedencia=i.procedencia or "não informada",
        queixa=h.queixa_principal, duracao=h.duracao_sintomas or "não informada",
        hda=h.hda, antecedentes=h.historico_previo or "Sem antecedentes.",
        hist_familiar=h.historia_familiar or "Sem história familiar relevante.",
        trat_prev=pr.tratamentos or "Nenhum.", medicamentos=pr.medicamentos or "Nenhum.",
        alergias=pr.alergias or "Nega alergias.",
        exame_geral=a.exame_geral, achados_esp=a.achados_especificos,
        sinais_vitais=a.sinais_vitais or "Não registrados.",
        timeline=_timeline(cko),
        lab=d.exames_lab or "Não realizados.", imagem=d.exames_imagem or "Não realizados.",
        diagnostico=d.diagnostico_definitivo,
        diferenciais=d.diferenciais or "Não especificados.",
        desafios=d.desafios or "Nenhum relatado.",
        tipo_intervencao=iv.tipo, desc_intervencao=iv.descricao, mudanca=mudanca,
        desfecho=de.desfecho_clinico, adesao=de.adesao or "Não avaliada.",
        tempo_acomp=de.tempo_acompanhamento or "Não especificado.",
        eventos_adv=de.eventos_adversos or "Nenhum.",
        perspectiva=cko.perspectiva_paciente or "Não coletada.",
        problema=ed.problemas_clinicos, diferencial=ed.diferencial_caso,
        periodico=ed.periodico or "Não especificado.", formato_ref=ed.formato_ref,
        referencias=_refs_txt(artigos),
    )

    resp = await chamar(_SYSTEM, prompt, complexidade=Complexidade.ALTA, max_tokens=8192)
    data = extrair_json(resp)

    refs_idx = set(data.get("referencias_usadas", range(1, len(artigos) + 1)))
    referencias = [
        Referencia(
            numero=art["numero"], autores=art.get("autores", ""),
            titulo=art.get("titulo", ""), periodico=art.get("periodico", ""),
            ano=art.get("ano", ""), volume=art.get("volume"),
            numero_edicao=str(art["numero_edicao"]) if art.get("numero_edicao") is not None else None,
            paginas=art.get("paginas"),
            doi=art.get("doi"), pmid=art.get("pmid"),
            formatada=art.get("formatada", ""),
        )
        for art in artigos if art["numero"] in refs_idx
    ]

    return ArtigoGerado(
        titulo=data["titulo"],
        palavras_chave=data.get("palavras_chave", []),
        resumo=Resumo(**data["resumo"]),
        introducao=data.get("introducao", []),
        caso_clinico=data.get("caso_clinico", []),
        discussao=data.get("discussao", []),
        conclusao=data.get("conclusao", []),
        referencias=referencias,
    )


# ── Demo mode ────────────────────────────────────────────────────────────────

_SYSTEM_DEMO = """Você é um redator científico especialista em relatos de caso clínico.
Escreva APENAS em português brasileiro formal e técnico.
Responda APENAS com JSON válido, sem markdown ou texto extra."""

_TEMPLATE_DEMO = """Redija a Introdução e a Apresentação do Caso clínico para um relato científico.

══ DADOS DO CASO ══
PACIENTE: {idade} {idade_unidade} | {sexo}
QUEIXA: {queixa} (duração: {duracao})
HDA: {hda}
ANTECEDENTES: {antecedentes}
DIAGNÓSTICO: {diagnostico}
INTERVENÇÃO: {desc_intervencao}
DESFECHO: {desfecho}

══ REFERÊNCIAS DISPONÍVEIS (use até 4) ══
{referencias}

══ INSTRUÇÕES ══
INTRODUÇÃO: 2 parágrafos ~200 palavras — contexto clínico e relevância do caso (cite referências com [N])
CASO CLÍNICO: narrativa cronológica ~350 palavras — apresentação do paciente, exames, diagnóstico e evolução

JSON de resposta:
{{
  "titulo": "Relato de Caso: <diagnóstico principal>",
  "introducao": ["P1", "P2"],
  "caso_clinico": ["P1", "P2", "P3"]
}}"""


async def executar_demo(cko: CKO, artigos: list[dict]) -> dict:
    """Pipeline de demonstração — gera apenas Introdução + Caso Clínico (~2 500 tokens)."""
    i = cko.identificacao
    h = cko.historia
    d = cko.diagnostico
    iv = cko.intervencao
    de = cko.desfechos
    pr = cko.intervencoes_anteriores

    # Limita referências a 4 para economizar tokens
    artigos_demo = artigos[:4]

    prompt = _TEMPLATE_DEMO.format(
        idade=i.idade or "não informada", idade_unidade=i.idade_unidade,
        sexo=i.sexo or "não informado",
        queixa=h.queixa_principal, duracao=h.duracao_sintomas or "não informada",
        hda=h.hda,
        antecedentes=h.historico_previo or "Sem antecedentes relevantes.",
        diagnostico=d.diagnostico_definitivo,
        desc_intervencao=iv.descricao,
        desfecho=de.desfecho_clinico,
        referencias=_refs_txt(artigos_demo) if artigos_demo else "Nenhuma disponível.",
    )

    resp = await chamar(_SYSTEM_DEMO, prompt, complexidade=Complexidade.ALTA, max_tokens=2500)
    data = extrair_json(resp)

    return {
        "titulo": data.get("titulo", f"Relato de Caso: {d.diagnostico_definitivo}"),
        "introducao": data.get("introducao", []),
        "caso_clinico": data.get("caso_clinico", []),
    }
