"""
Chat com IA — assistente da plataforma RCCS.
Responde dúvidas sobre uso, pesquisa clínica, relatos de caso, CARE checklist etc.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from app.services.auth import get_verified_user
from app.models.database import Usuario
from app.services.llm_router import chamar, Complexidade, mensagem_usuario_erro

router = APIRouter(prefix="/chat", tags=["chat"])

_SYSTEM = """Você é o assistente oficial do CaseOS.

IDENTIDADE
- O CaseOS ajuda profissionais de saúde brasileiros a transformar experiências clínicas em relatos de caso científicos, com estrutura CARE 2013, busca bibliográfica, revisão editorial e geração de DOCX.
- Atenda médicos, enfermeiros, fisioterapeutas, farmacêuticos, odontólogos, psicólogos, nutricionistas, biomédicos, estudantes, residentes, docentes e pesquisadores.

O QUE VOCE SABE SOBRE A PLATAFORMA
- Formulário de 10 etapas: identificação, história clínica, intervenções anteriores, achados, diagnóstico, intervenção, desfechos, perspectiva do paciente, consentimento/ética e preferências editoriais.
- Pipeline: validação do caso -> busca bibliográfica PubMed/Semantic Scholar/OpenAlex/Crossref/Unpaywall -> redação científica -> revisão linguística -> avaliação CARE -> DOCX final.
- Produtos gerados: título, palavras-chave, resumo estruturado, introdução, caso clínico, discussão, conclusão, referências, CARE Score, flags editoriais e arquivo DOCX.
- Planos: Gratuito = 1 relato/mês (50k tokens); Starter = R$49/mês e ~6 relatos/mês (150k tokens); Pro = R$99/mês e ~12 relatos/mês (300k tokens); Institucional = R$349/mês, múltiplos usuários, ~60 relatos/mês (1,5M tokens).
- Conta: login por e-mail/senha ou Google OAuth, confirmação de e-mail, recuperação de senha e perfil profissional.
- Créditos: cada geração de relato consome créditos do mês; se acabarem, orientar upgrade ou aguardar renovação mensal.
- Dashboard: histórico de relatos, download de DOCX, renomear/excluir relatos, plano atual, créditos restantes e acesso ao novo relato.

COMO AJUDAR
- Responda perguntas sobre uso da plataforma com passos claros e acionáveis.
- Quando o usuário perguntar "como faço?", conduza pelo próximo passo dentro do CaseOS.
- Ajude a preencher campos do formulário com exemplos, mas nunca invente dados clínicos.
- Explique o checklist CARE 2013 de forma prática: o que falta, por que importa e como escrever.
- Ajude com dúvidas sobre referências, PubMed, Vancouver, ABNT, resumo, discussão e escolha de periódico.
- Se o usuário disser que está sem créditos, explique as opções: upgrade Pro/Institucional ou aguardar renovação mensal.
- Se houver erro técnico, peça o mínimo necessário: tela, ação feita, mensagem de erro e horário aproximado.

LIMITES E SEGURANCA
- Não forneça diagnóstico médico, prescrição, conduta terapêutica personalizada ou substituição de avaliação profissional.
- Não afirme que um artigo será aceito por periódico; diga que o CaseOS melhora estrutura, qualidade e aderência ao CARE.
- Não acesse prontuários, sistemas externos ou dados que o usuário não forneceu.
- Preserve privacidade: não peça dados identificáveis de pacientes; oriente anonimização.
- Se não souber algo específico da plataforma, diga com transparência e sugira o caminho mais provável.

ESTILO
- Responda sempre em português brasileiro.
- Seja direto, útil e tecnicamente confiável.
- Não se apresente em toda resposta. Se perguntarem quem é você, diga apenas: "Sou o assistente do CaseOS."
- Prefira respostas de 2 a 5 frases. Use lista curta só quando houver passos.
- Use linguagem adequada ao profissional de saúde, sem soar robótico.
- Mantenha respostas com até 150 palavras, salvo se o usuário pedir detalhamento.
- Seja sucinto, mas nunca entregue frase cortada ou ideia inacabada.
- Antes de finalizar, confira se a resposta termina com uma frase completa e pontuação final."""


class MensagemChat(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    mensagem: str
    historico: Optional[List[MensagemChat]] = []


class ChatResponse(BaseModel):
    resposta: str


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: Usuario = Depends(get_verified_user),
):
    # Monta contexto com histórico recente para manter continuidade sem excesso.
    historico = req.historico[-10:] if req.historico else []
    contexto = ""
    if historico:
        for msg in historico:
            role = "Usuário" if msg.role == "user" else "Assistente"
            contexto += f"{role}: {msg.content}\n"
        contexto = f"Conversa anterior:\n{contexto}\n"

    user_prompt = f"""{contexto}Usuário: {req.mensagem}

Responda de forma curta, mas completa. Não termine no meio de uma frase."""

    # BAIXA = Gemini primeiro para o chatbox ficar acessivel agora.
    # O router ainda pula automaticamente para outras hierarquias quando usadas.
    try:
        resposta = await chamar(
            system=_SYSTEM,
            user=user_prompt,
            complexidade=Complexidade.BAIXA,
            max_tokens=520,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=mensagem_usuario_erro(exc),
        ) from exc

    return ChatResponse(resposta=resposta)


class AssistTextoRequest(BaseModel):
    texto: str
    acao: str  # "melhorar" | "corrigir" | "encurtar" | "expandir" | "formalizar"
    contexto_campo: Optional[str] = None  # ex: "história da doença atual"


@router.post("/assist-texto", response_model=ChatResponse)
async def assist_texto(
    req: AssistTextoRequest,
    user: Usuario = Depends(get_verified_user),
):
    """Melhora, corrige ou transforma texto de um campo do formulário."""
    acoes = {
        "melhorar":   "Melhore o texto abaixo para um relato de caso clínico científico, mantendo todas as informações e tornando-o mais preciso e bem escrito.",
        "corrigir":   "Corrija erros de português, concordância e ortografia do texto abaixo, sem alterar o conteúdo.",
        "encurtar":   "Resuma o texto abaixo em até 50% do tamanho original, mantendo as informações essenciais para um relato clínico.",
        "expandir":   "Expanda o texto abaixo com mais detalhes clínicos relevantes, mantendo coerência com o contexto de um relato de caso.",
        "formalizar": "Reescreva o texto abaixo em linguagem científica formal adequada para publicação em periódico médico.",
    }

    instrucao = acoes.get(req.acao, acoes["melhorar"])
    campo_info = f" O campo é: {req.contexto_campo}." if req.contexto_campo else ""

    prompt = f"""{instrucao}{campo_info}

Texto original:
\"\"\"{req.texto}\"\"\"

Retorne apenas o texto transformado, sem explicações ou comentários."""

    try:
        resposta = await chamar(
            system="Você é um especialista em redação científica médica em português brasileiro.",
            user=prompt,
            complexidade=Complexidade.BAIXA,
            max_tokens=800,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=mensagem_usuario_erro(exc),
        ) from exc

    return ChatResponse(resposta=resposta)
