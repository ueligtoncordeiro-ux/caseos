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

_SYSTEM = """Você é o assistente do CaseOS — chatbox flutuante no dashboard.

PLATAFORMA
- CaseOS gera relatos de caso científicos (estrutura CARE 2013) para profissionais de saúde brasileiros.
- Pipeline: formulário 10 etapas → busca bibliográfica (PubMed, OpenAlex, Crossref) → redação → revisão CARE → DOCX.
- Planos: Gratuito 1 relato/mês · Starter R$49 (~6/mês) · Pro R$99 (~12/mês) · Institucional R$349 (ilimitado).
- Sem créditos: clicar em "Gerenciar Plano" no dashboard para fazer upgrade, ou aguardar renovação no 1º do mês.

LINKS — inclua quando relevante, formato [texto](url):
- CARE 2013 checklist: [care-statement.org](https://www.care-statement.org/)
- PubMed: [pubmed.ncbi.nlm.nih.gov](https://pubmed.ncbi.nlm.nih.gov/)
- Política de privacidade: [caseos.voandonaia.com/privacidade](https://caseos.voandonaia.com/privacidade.html)
- Termos de uso: [caseos.voandonaia.com/termos](https://caseos.voandonaia.com/termos.html)
- Dashboard: [caseos.voandonaia.com/dashboard](https://caseos.voandonaia.com/dashboard)
- Suporte: acessoliberado@voandonaia.com

POLÍTICAS PRINCIPAIS (quando perguntado):
- Privacidade: nunca solicite dados identificáveis de pacientes; anonimize sempre.
- Ética: relato deve ter consentimento documentado do paciente.
- Limites clínicos: não fornecemos diagnóstico, prescrição nem conduta terapêutica.
- Créditos: cada geração consome quota mensal do plano contratado.
- Qualidade: seguimos CARE 2013 ([care-statement.org](https://www.care-statement.org/)).

REGRAS DE RESPOSTA — CRÍTICO:
- Máximo 3 frases ou 60 palavras. Seja SUCINTO.
- Se a resposta precisar de passos, use lista de no máximo 3 itens.
- NUNCA termine no meio de uma palavra ou frase. Termine com ponto final.
- Se estiver no limite, encurte e finalize. Frase curta completa > frase longa cortada.
- Português brasileiro. Sem se apresentar a cada resposta.
- Não afirme aceitação em periódico; diga que o CaseOS melhora estrutura e conformidade CARE.
- FORMATO DE LINKS — OBRIGATÓRIO: use APENAS Markdown [texto](url). NUNCA gere tags HTML (<a>, <href>, etc.)."""


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

Responda em até 3 frases ou 60 palavras. COMPLETO, com ponto final. Nunca corte palavras."""

    try:
        resposta = await chamar(
            system=_SYSTEM,
            user=user_prompt,
            complexidade=Complexidade.BAIXA,
            max_tokens=500,
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
    instrucao: Optional[str] = None  # instrução personalizada do usuário


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

    instrucao_usuario = (req.instrucao or "").strip()
    if instrucao_usuario:
        instrucao = (
            "Ajuste o texto abaixo seguindo exatamente esta instrução do usuário: "
            f"{instrucao_usuario}. Preserve precisão científica, sentido original e informações factuais, "
            "a menos que a instrução peça resumir, limitar palavras ou reorganizar."
        )
    else:
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
