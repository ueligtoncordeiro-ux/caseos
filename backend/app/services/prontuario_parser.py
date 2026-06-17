"""
Prontuário Parser — extrai campos CKO de texto clínico livre.

Recebe um texto não estruturado (evolução, resumo de alta, nota de prontuário)
e retorna um dict com os campos CKO identificados, prontos para pré-preencher
o formulário novo-relato.html.

Usa GPT-4o mini (MEDIA) — boa relação custo/precisão para extração de dados.
Falhas retornam dict vazio (formulário permanece em branco).
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM = """\
Você é um extrator de informações clínicas. Analise o texto de prontuário e \
extraia os dados estruturados solicitados.

REGRAS CRÍTICAS:
1. Extraia APENAS o que está explicitamente no texto — não invente dados.
2. Se um campo não estiver no texto, omita-o do JSON (não use null ou "").
3. Preserve o idioma original do texto nos campos extraídos.
4. Para eventos da timeline, extraia em ordem cronológica.
5. Anonimize automaticamente: substitua nomes de pessoas por "[paciente]" \
   e documentos por "[omitido]".
6. Responda APENAS com JSON válido, sem markdown, sem explicações.\
"""

_TEMPLATE = """\
Extraia os seguintes campos clínicos do texto abaixo e retorne como JSON.
Omita campos ausentes. Não invente informações.

CAMPOS A EXTRAIR:
{{
  "idade": "número (anos ou meses)",
  "idade_unidade": "anos|meses|dias",
  "sexo": "Masculino|Feminino|Outro",
  "procedencia": "cidade/estado de origem",
  "outras_infos": "outras informações demográficas relevantes",

  "queixa_principal": "queixa principal em 1-2 frases",
  "duracao_sintomas": "tempo de evolução (ex: 3 dias, 2 semanas)",
  "hda": "história da doença atual detalhada",
  "historico_previo": "antecedentes pessoais relevantes",
  "historia_familiar": "antecedentes familiares relevantes",

  "tratamentos": "tratamentos anteriores ao atendimento atual",
  "medicamentos": "medicamentos em uso na admissão",
  "alergias": "alergias conhecidas",

  "sinais_vitais": "sinais vitais ao exame (PA, FC, FR, Temp, SpO2)",
  "exame_geral": "achados ao exame físico geral",
  "achados_especificos": "achados específicos ao exame segmentar",

  "eventos_timeline": [
    {{"data": "data ou período", "evento": "descrição do evento", "tipo": "sintoma|consulta|exame|tratamento|desfecho"}}
  ],

  "exames_lab": "resultados de exames laboratoriais",
  "exames_imagem": "resultados de exames de imagem",
  "diagnostico_definitivo": "diagnóstico final confirmado",
  "diferenciais": "diagnósticos diferenciais considerados",
  "desafios": "dificuldades ou desafios no diagnóstico",
  "prognostico": "prognóstico documentado",

  "intervencao_tipo": "tipo de intervenção (cirúrgica/farmacológica/etc.)",
  "intervencao_descricao": "descrição detalhada da intervenção",
  "houve_mudanca": true|false,
  "desc_mudanca": "descrição de mudanças na conduta (se houve)",

  "desfecho_clinico": "desfecho clínico final",
  "exames_seguimento": "exames realizados no seguimento",
  "adesao": "informação sobre adesão ao tratamento",
  "eventos_adversos": "eventos adversos ou complicações",

  "perspectiva_paciente": "relato do paciente sobre sua experiência"
}}

TEXTO DO PRONTUÁRIO:
---
{texto}
---

Retorne apenas o JSON com os campos encontrados.\
"""


async def extrair(texto: str) -> dict[str, Any]:
    """
    Extrai campos CKO de texto clínico livre.
    Retorna dict parcial — campos ausentes são omitidos.
    Nunca lança exceção: erros retornam dict vazio + campo _erro.
    """
    from app.services.llm_router import chamar, extrair_json, Complexidade

    texto_limpo = texto.strip()[:6000]   # limita tamanho do contexto

    prompt = _TEMPLATE.format(texto=texto_limpo)

    try:
        resp = await chamar(
            _SYSTEM, prompt,
            complexidade=Complexidade.MEDIA,
            max_tokens=1500,
            json_mode=True,
        )
        dados = extrair_json(resp)

        # Sanitização básica — remove campos com valores vazios/inválidos
        return {k: v for k, v in dados.items() if v not in (None, "", [], {})}

    except Exception as exc:
        logger.warning("prontuario_parser: falha na extração: %s", exc)
        return {"_erro": str(exc)[:200]}
