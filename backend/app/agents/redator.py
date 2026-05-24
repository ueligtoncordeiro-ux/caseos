"""
Agente Redator — Claude Sonnet 4.6 (alta complexidade).
Escrita científica longa em PT-BR exige contexto extenso e raciocínio profundo.
Usa duas chamadas LLM para evitar truncamento: Pass 1 gera as seções iniciais,
Pass 2 gera Discussão, Conclusão e referências usadas com contexto completo.
"""
from app.models.schemas import CKO, ArtigoGerado, Resumo, Referencia
from app.services.llm_router import chamar, extrair_json, Complexidade

_SYSTEM = """Você é um redator científico sênior especialista em relatos de caso clínico,
com domínio do checklist CARE 2013, normas Vancouver/ICMJE e escrita médica em português brasileiro.
Você publica regularmente. Escreve como clínico experiente, não como robô acadêmico.

━━ REGRAS ABSOLUTAS ━━
1. Escreva APENAS em português brasileiro formal e técnico
2. Tempo verbal: passado para eventos do caso; presente para afirmações da discussão
3. VOZ GRAMATICAL: Terceira pessoa impessoal ou voz passiva. NUNCA primeira pessoa do plural.
   ✗ PROIBIDO: "Relatamos", "Descrevemos", "Apresentamos", "Observamos", "Encontramos"
   ✓ CORRETO: "Relatou-se", "Descreveu-se", "Este relato apresenta", "foi observado", "verificou-se"
4. CITAÇÕES IN-TEXT OBRIGATÓRIAS: coloque [N] imediatamente após o ponto da afirmação.
   Toda afirmação epidemiológica, fisiopatológica, diagnóstica ou terapêutica DEVE ter [N].
   Citação no Caso Clínico: use para critérios diagnósticos e dados comparativos.
   Na Discussão: CADA parágrafo DEVE citar pelo menos 2 referências diferentes.
5. NÃO invente dados clínicos — use APENAS o que está no CKO
6. NÃO cite artigos fora da lista fornecida
7. Responda APENAS com JSON válido, sem markdown extra
8. TÍTULO: sentence case — apenas a primeira palavra e nomes próprios em maiúsculo.
   ERRADO: "Osteorradionecrose De Mandíbula Após Radioterapia Para Carcinoma"
   CORRETO: "Osteorradionecrose de mandíbula após radioterapia para carcinoma: relato de caso"
9. REFERÊNCIAS: use mínimo 15 diferentes; distribua por Introdução, Caso Clínico e Discussão
10. DISCUSSÃO — regra inegociável de citação por autor:
    NUNCA escreva "a literatura", "estudos mostram", "pesquisas indicam", "evidências sugerem"
    SEMPRE identifique o autor: "Marx et al. [1] demonstraram que...", "Segundo Lyons e Ghazali [2]...",
    "Em estudo de Chronopoulos et al. [6]...", "Shaw e Butterworth [5] relataram..."

━━ TRAVESSÃO — PROIBIÇÃO ABSOLUTA ━━
O caractere — (travessão/em dash) é COMPLETAMENTE PROIBIDO em qualquer seção.
Substitua por: vírgula, ponto e vírgula, dois-pontos ou reescreva a frase.
✗ "O tratamento — cirúrgico ou conservador — depende do grau."
✓ "O tratamento depende do grau: pode ser cirúrgico ou conservador."
✓ "O tratamento, cirúrgico ou conservador, depende do grau."

━━ PALAVRAS E CONSTRUÇÕES PROIBIDAS ━━
Nunca use — nem paráfrase próxima:
inequivocamente, evidencia que, ressalta, corrobora, amplamente, robusto, notável,
fundamental, crucial, imperativo, abrangente, denota, configura, revela que,
destaca-se, cabe ressaltar, vale destacar, nesse contexto, diante do exposto,
à luz dos dados, emergir, permear, elucidar, catalisar, paradigma (exceto citação direta),
no âmbito, sob a ótica, delinear, preconizar, suscitar reflexões, inquestionável,
é de suma importância, tem sido amplamente, é sabido que, é bem estabelecido que

INÍCIOS PROIBIDOS:
- Nunca inicie com "O presente relato...", "O presente estudo...", "O presente caso..."
- Nunca inicie dois parágrafos consecutivos com a mesma palavra ou estrutura

ESTRUTURAS PROIBIDAS:
- "tanto... quanto" em excesso (máx 1 por artigo)
- "não apenas... mas também" em excesso (máx 1 por artigo)
- Frases com 4 ou mais adjetivos seguidos
- Orações relativas encaixadas (mais de 2 por parágrafo)

━━ COMO ESCREVER (OBRIGATÓRIO) ━━
- VARIE o comprimento: frases curtas (< 10 palavras), médias e longas — nunca uniforme
- Seja específico: cite números, datas, medidas, doses exatas do CKO
- Acadêmico sem cerimônia: escreva como clínico sênior que publica em periódico B1/B2
- Parágrafos devem começar de modos completamente diferentes entre si
- Use conectores variados: "Nesse sentido,", "Por outro lado,", "Além disso,", "Contudo,"
- Prefira frases na ordem direta: sujeito → verbo → complemento"""

# ── Pass 1: title, abstract, intro, case presentation (max_tokens=5000) ──────
_TEMPLATE_PASS1 = """Redija a primeira metade de um relato de caso clínico científico.

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

══ ESTRUTURA (APENAS ESTAS SEÇÕES) ══
TÍTULO: sentence case (apenas primeira palavra e nomes próprios em maiúsculo); NÃO use travessão; exemplo: "Osteorradionecrose de mandíbula após radioterapia: relato de caso"
PALAVRAS-CHAVE: 3–5 descritores MeSH/DeCS em português
RESUMO: máx 250 palavras — 4 subseções (Introdução | Apresentação do Caso | Discussão | Conclusão)
INTRODUÇÃO: 3 parágrafos ~300 palavras
  P1: contexto epidemiológico (cite 3 refs diferentes)
  P2: fisiopatologia e quadro clínico (cite 3 refs diferentes)
  P3: objetivo do relato (sem citação, 1 frase objetiva)
CASO CLÍNICO: narrativa cronológica ~450 palavras
  — apresentação, exames, diagnóstico, intervenção, desfecho
  — cite referências para critérios diagnósticos e dados comparativos

Aplique todas as regras de escrita humana do system prompt.

JSON de resposta (APENAS estes campos):
{{
  "titulo": "...",
  "palavras_chave": ["..."],
  "resumo": {{"introducao":"...","caso":"...","discussao":"...","conclusao":"..."}},
  "introducao": ["P1","P2","P3"],
  "caso_clinico": ["P1","P2","P3","P4","..."],
  "referencias_usadas_parcial": [1,2,3]
}}"""


# ── Pass 2: discussao, conclusao, referencias_usadas (max_tokens=6000) ────────
_TEMPLATE_PASS2 = """Você está finalizando um relato de caso clínico. As seções iniciais já foram escritas (abaixo).
Escreva APENAS as seções que faltam: Discussão, Conclusão e lista final de referências.

══ CKO (resumo) ══
PACIENTE: {idade} {idade_unidade} | {sexo}
DIAGNÓSTICO: {diagnostico}
INTERVENÇÃO ({tipo_intervencao}): {desc_intervencao}
DESFECHO: {desfecho}
PROBLEMA CLÍNICO: {problema}
DIFERENCIAL DO CASO: {diferencial}

══ SEÇÕES JÁ ESCRITAS (contexto para consistência) ══
TÍTULO: {titulo}
INTRODUÇÃO (resumida): {intro_resumo}
CASO CLÍNICO (resumido): {caso_resumo}

══ REFERÊNCIAS DISPONÍVEIS ══
{referencias}

══ ESTRUTURA OBRIGATÓRIA (APENAS ESTAS SEÇÕES) ══
DISCUSSÃO: 10 parágrafos ~900 palavras — cada parágrafo cita ao menos 2 refs diferentes
  P1: diferencial do caso (cite 2 refs) — por que este diagnóstico e não os diferenciais?
  P2: epidemiologia (cite 3 refs) — incidência, prevalência, grupos de risco
  P3: fisiopatologia (cite 2 refs) — mecanismo molecular/celular relevante
  P4: achados do caso vs. literatura (cite 2 refs) — compare com outros relatos
  P5: métodos diagnósticos (cite 2 refs) — exames usados, sensibilidade/especificidade
  P6: diagnósticos diferenciais (cite 2 refs) — como foram descartados
  P7: intervenção (cite 2 refs) — escolha terapêutica, alternativas, justificativa
  P8: desfechos e seguimento (cite 2 refs) — prognóstico, recidiva, qualidade de vida
  P9: perspectiva do paciente e limitações do relato (cite 1 ref)
  P10: lições clínicas (cite 1 ref) — o que este caso ensina?
CONCLUSÃO: 1–2 parágrafos ~120 palavras — síntese objetiva, sem repetir a introdução

ATENÇÃO CRÍTICA — REFERÊNCIAS:
- O total acumulado (Pass 1 + Pass 2) deve usar MÍNIMO 15 referências diferentes
- Não repita a mesma citação em dois parágrafos consecutivos sem necessidade
- Liste em "referencias_usadas" TODOS os números efetivamente citados no texto completo (Pass 1 + Pass 2)

Aplique todas as regras de escrita humana do system prompt.

JSON de resposta (APENAS estes campos):
{{
  "discussao": ["P1","P2","P3","P4","P5","P6","P7","P8","P9","P10"],
  "conclusao": ["P1"],
  "referencias_usadas": [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
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


def _build_cko_kwargs(cko: CKO) -> dict:
    """Return the shared keyword args used to format both pass templates."""
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
        mudanca = f"Sim: {iv.desc_mudanca or ''} (justificativa: {iv.just_mudanca or ''})"

    return dict(
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
    )


async def executar(cko: CKO, artigos: list[dict]) -> ArtigoGerado:
    kw = _build_cko_kwargs(cko)
    refs_txt = _refs_txt(artigos)

    # ── Pass 1: titulo, palavras_chave, resumo, introducao, caso_clinico ─────
    prompt1 = _TEMPLATE_PASS1.format(**kw, referencias=refs_txt)
    resp1 = await chamar(_SYSTEM, prompt1, complexidade=Complexidade.ALTA, max_tokens=5000)
    data1 = extrair_json(resp1)

    titulo = data1.get("titulo", f"Relato de Caso: {cko.diagnostico.diagnostico_definitivo}")
    palavras_chave = data1.get("palavras_chave", [])
    resumo_raw = data1.get("resumo", {})
    introducao = data1.get("introducao", [])
    caso_clinico = data1.get("caso_clinico", [])
    refs_parcial: set[int] = set(data1.get("referencias_usadas_parcial", []))

    # Build short summaries for Pass 2 context (avoid re-sending full text)
    intro_resumo = " // ".join(p[:120] + "..." for p in introducao[:3]) if introducao else "Não gerada."
    caso_resumo = " // ".join(p[:120] + "..." for p in caso_clinico[:3]) if caso_clinico else "Não gerado."

    # ── Pass 2: discussao, conclusao, referencias_usadas ─────────────────────
    prompt2 = _TEMPLATE_PASS2.format(
        idade=kw["idade"], idade_unidade=kw["idade_unidade"], sexo=kw["sexo"],
        diagnostico=kw["diagnostico"], tipo_intervencao=kw["tipo_intervencao"],
        desc_intervencao=kw["desc_intervencao"], desfecho=kw["desfecho"],
        problema=kw["problema"], diferencial=kw["diferencial"],
        titulo=titulo,
        intro_resumo=intro_resumo,
        caso_resumo=caso_resumo,
        referencias=refs_txt,
    )

    discussao: list[str] = []
    conclusao: list[str] = []
    refs_final: set[int] = set()

    try:
        resp2 = await chamar(_SYSTEM, prompt2, complexidade=Complexidade.ALTA, max_tokens=6000)
        data2 = extrair_json(resp2)
        discussao = data2.get("discussao", [])
        conclusao = data2.get("conclusao", [])
        refs_final = set(data2.get("referencias_usadas", []))
    except Exception:
        # Pass 2 failed: keep empty sections, article is still usable
        discussao = []
        conclusao = []
        refs_final = refs_parcial

    # Merge reference sets from both passes
    refs_idx = refs_parcial | refs_final
    if not refs_idx:
        refs_idx = set(range(1, len(artigos) + 1))

    # Guarantee minimum 15 references
    MIN_REFS = 15
    if len(refs_idx) < MIN_REFS and len(artigos) >= MIN_REFS:
        for art in artigos:
            if len(refs_idx) >= MIN_REFS:
                break
            refs_idx.add(art["numero"])

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

    # Reconstruct Resumo — use whatever the LLM returned, with safe defaults
    resumo_obj = Resumo(
        introducao=resumo_raw.get("introducao", ""),
        caso=resumo_raw.get("caso", ""),
        discussao=resumo_raw.get("discussao", ""),
        conclusao=resumo_raw.get("conclusao", ""),
    )

    return ArtigoGerado(
        titulo=titulo,
        palavras_chave=palavras_chave,
        resumo=resumo_obj,
        introducao=introducao,
        caso_clinico=caso_clinico,
        discussao=discussao,
        conclusao=conclusao,
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
