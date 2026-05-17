"""
Chat com IA — assistente da plataforma RCCS.
Responde dúvidas sobre uso, pesquisa clínica, relatos de caso, CARE checklist etc.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import List, Optional

from app.services.auth import get_verified_user
from app.models.database import Usuario
from app.services.llm_router import chamar, Complexidade

router = APIRouter(prefix="/chat", tags=["chat"])

_SYSTEM = """Você é o assistente do neuraxIA CaseOS, \
uma plataforma de IA para médicos e pesquisadores brasileiros criarem relatos de caso clínico \
com qualidade para publicação científica. neuraxIA é a empresa, CaseOS é o produto.

Seu papel:
- Ajudar o usuário a entender como usar a plataforma
- Tirar dúvidas sobre relatos de caso clínico
- Explicar o checklist CARE 2013
- Dar dicas de como preencher os campos do formulário
- Orientar sobre escolha de periódicos para submissão
- Sugerir como melhorar a qualidade do caso clínico
- Responder dúvidas sobre metodologia de relatos de caso
- Explicar planos e funcionalidades da plataforma

Seja sempre objetivo, amigável e use linguagem médica adequada mas acessível.
Responda sempre em português brasileiro.
Nunca forneça diagnósticos médicos ou recomendações terapêuticas ao usuário (apenas oriente \
sobre como documentar casos que já ocorreram).
Mantenha respostas concisas (máximo 300 palavras salvo pedido de detalhamento)."""


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
    # Monta contexto com histórico (últimas 6 mensagens)
    historico = req.historico[-6:] if req.historico else []
    contexto = ""
    if historico:
        for msg in historico:
            role = "Usuário" if msg.role == "user" else "Assistente"
            contexto += f"{role}: {msg.content}\n"
        contexto = f"Conversa anterior:\n{contexto}\n"

    user_prompt = f"{contexto}Usuário: {req.mensagem}"

    resposta = await chamar(
        system=_SYSTEM,
        user=user_prompt,
        complexidade=Complexidade.MEDIA,
        max_tokens=600,
    )

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

    resposta = await chamar(
        system="Você é um especialista em redação científica médica em português brasileiro.",
        user=prompt,
        complexidade=Complexidade.MEDIA,
        max_tokens=800,
    )

    return ChatResponse(resposta=resposta)
