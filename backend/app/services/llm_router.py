"""
LLM Router — roteamento por complexidade com fallback em cascata.

Hierarquia definida (aplicada apenas se a chave estiver configurada):
  ALTA complexidade  → Claude Sonnet 4.6  → GPT-4o       → Gemini (fallback)
  MÉDIA complexidade → GPT-4o mini        → Claude Haiku → Gemini (fallback)
  BAIXA complexidade → GPT-4o mini        → Claude Haiku → Gemini (fallback)

Regra de disponibilidade:
  • Providers sem chave configurada são automaticamente ignorados.
  • OpenAI e Claude são os provedores primários; Gemini é fallback opcional.
  • Basta adicionar/remover chaves no .env para ativar/desativar providers.

Custo estimado por artigo (com OpenAI + Claude ativos):
  Claude Sonnet 4.6  ~$0.35   ← geração de artigos (ALTA)
  GPT-4o mini        ~$0.02   ← revisão, chatbox, assist (MEDIA / BAIXA)
  Gemini 2.5 Flash   opcional ← fallback se ANTHROPIC + OPENAI indisponíveis
"""
import json
import logging
import asyncio
import contextvars
from enum import Enum
from typing import Any, Optional

import anthropic
import openai
import google.generativeai as genai

from app.config import settings

logger = logging.getLogger(__name__)

# ── Contador de tokens por pipeline (ContextVar — thread/task safe) ───────────
_session_tokens: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "session_tokens", default=None
)


def iniciar_contagem_tokens() -> dict:
    """Chame no início do pipeline. Retorna o dict mutável do contador."""
    counter: dict = {"total": 0, "claude": 0, "openai": 0, "gemini": 0}
    _session_tokens.set(counter)
    return counter


def obter_tokens_usados() -> int:
    """Retorna o total acumulado desde iniciar_contagem_tokens()."""
    c = _session_tokens.get(None)
    return c["total"] if c else 0


def _registrar_tokens(n: int, provider: str = "outro") -> None:
    """Interno: adiciona n tokens ao contador ativo."""
    c = _session_tokens.get(None)
    if c is not None and n > 0:
        c["total"] += n
        c[provider] = c.get(provider, 0) + n


class LLMQuotaError(RuntimeError):
    """Erro de quota/billing do provedor de IA."""


def mensagem_usuario_erro(exc: Exception) -> str:
    texto = str(exc)
    if "429" in texto or "quota" in texto.lower() or "rate" in texto.lower():
        return (
            "A IA está configurada, mas a cota do provedor foi atingida. "
            "Verifique billing/quota da OpenAI ou Anthropic no Render e tente novamente."
        )
    if "OPENAI_API_KEY" in texto or "ANTHROPIC_API_KEY" in texto:
        return "Chave da OpenAI ou Anthropic não configurada no ambiente de produção."
    if "Nenhum provider" in texto:
        return "Nenhum provedor de IA está configurado. Configure OPENAI_API_KEY ou ANTHROPIC_API_KEY no Render."
    return "Assistente indisponível no momento. Tente novamente em instantes."


class Complexidade(str, Enum):
    ALTA  = "alta"
    MEDIA = "media"
    BAIXA = "baixa"


# ── Disponibilidade de chaves ─────────────────────────────────────────────────

def _tem_chave(provider: str) -> bool:
    """Retorna True se a chave do provider parecer real e utilizavel."""
    mapa = {
        "claude": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key,
    }
    valor = (mapa.get(provider) or "").strip()
    if not valor:
        return False

    # Evita tentar providers preenchidos com placeholders do .env.example.
    placeholders = {
        "sk-",
        "sk-...",
        "sk-ant-...",
        "AIza...",
        "xxxx",
        "sua-chave",
        "sua_chave",
        "your-key",
        "your_key",
    }
    normalizado = valor.lower()
    return (
        valor not in placeholders
        and "..." not in valor
        and "troque" not in normalizado
        and "example" not in normalizado
        and "exemplo" not in normalizado
    )


# ── Clientes (singletons lazy — reutilizam connection pool entre chamadas) ─────
# Criados uma única vez por processo: evita overhead de handshake TLS e
# overhead de criação de httpx.AsyncClient a cada chamada LLM.

_claude_client: anthropic.AsyncAnthropic | None = None
_openai_client: openai.AsyncOpenAI | None = None


def _claude() -> anthropic.AsyncAnthropic:
    global _claude_client
    if not _tem_chave("claude"):
        raise RuntimeError("ANTHROPIC_API_KEY não configurada.")
    if _claude_client is None:
        _claude_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _claude_client


def _openai_async() -> openai.AsyncOpenAI:
    global _openai_client
    if not _tem_chave("openai"):
        raise RuntimeError("OPENAI_API_KEY não configurada.")
    if _openai_client is None:
        _openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


def _gemini_model(model: str = "gemini-2.5-flash") -> genai.GenerativeModel:
    if not _tem_chave("gemini"):
        raise RuntimeError("GEMINI_API_KEY não configurada.")
    genai.configure(api_key=settings.gemini_api_key)
    return genai.GenerativeModel(model)


# ── Chamadas individuais ───────────────────────────────────────────────────────

async def _chamar_claude(system: str, user: str, max_tokens: int) -> str:
    client = _claude()
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # Registra tokens reais retornados pela API
    if hasattr(msg, "usage") and msg.usage:
        _registrar_tokens(
            (msg.usage.input_tokens or 0) + (msg.usage.output_tokens or 0),
            "claude"
        )
    return msg.content[0].text.strip()


async def _chamar_openai(
    system: str, user: str, max_tokens: int,
    json_mode: bool = False, model: str = "gpt-4o-mini"
) -> str:
    client = _openai_async()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = await client.chat.completions.create(**kwargs)
    if hasattr(resp, "usage") and resp.usage:
        _registrar_tokens(resp.usage.total_tokens or 0, "openai")
    return resp.choices[0].message.content.strip()


async def _chamar_gemini(
    system: str, user: str, max_tokens: int, model: Optional[str] = None
) -> str:
    prompt = f"{system}\n\n{user}"
    if model:
        modelos = [model]
    else:
        candidatos = [settings.gemini_model]
        candidatos.extend(
            m.strip()
            for m in (settings.gemini_fallback_models or "").split(",")
            if m.strip()
        )
        modelos = list(dict.fromkeys(candidatos))
    erros = []
    for nome_modelo in modelos:
        try:
            m = _gemini_model(nome_modelo)
            resp = await m.generate_content_async(
                prompt,
                generation_config={"max_output_tokens": max_tokens},
            )
            texto = resp.text.strip()

            # Detecta resposta truncada pelo Gemini (finish_reason MAX_TOKENS)
            # Nesse caso tenta o próximo modelo ao invés de retornar texto cortado.
            try:
                finish = resp.candidates[0].finish_reason
                # finish_reason 2 = MAX_TOKENS no enum do Gemini SDK
                if finish and str(finish) in ("2", "MAX_TOKENS", "FinishReason.MAX_TOKENS"):
                    erros.append(f"{nome_modelo}: resposta truncada (MAX_TOKENS)")
                    continue  # tenta próximo modelo/fallback
            except Exception:
                pass  # campo não disponível — segue normalmente

            # Gemini não retorna usage de forma confiável — estima por caracteres
            estimado = (len(prompt) + len(texto)) // 4
            _registrar_tokens(estimado, "gemini")
            return texto
        except Exception as exc:
            erros.append(f"{nome_modelo}: {exc}")
            texto_exc = str(exc).lower()
            if "quota" in texto_exc or "429" in texto_exc or "rate limit" in texto_exc:
                logger.warning("Gemini quota/rate-limit em %s: %s", nome_modelo, exc)
                continue
    raise RuntimeError("Gemini falhou em todos os modelos: " + " | ".join(erros))


# ── Roteador principal com fallback em cascata ────────────────────────────────

async def chamar(
    system: str,
    user: str,
    complexidade: Complexidade = Complexidade.ALTA,
    max_tokens: int = 8192,
    json_mode: bool = False,
) -> str:
    """
    Executa a chamada LLM seguindo a hierarquia de complexidade.
    Providers sem chave configurada são ignorados automaticamente.

    Hierarquia de prioridade:
      ALTA  → Claude Sonnet 4.6 › GPT-4o      › Gemini (opt.)
      MÉDIA → GPT-4o mini       › Claude Haiku › Gemini (opt.)
      BAIXA → GPT-4o mini       › Claude Haiku › Gemini (opt.)
    """
    # Cada item: (nome_display, provider_key, callable)
    if complexidade == Complexidade.ALTA:
        candidatos = [
            ("Claude Sonnet 4.6", "claude",
             lambda: _chamar_claude(system, user, max_tokens)),
            ("GPT-4o",            "openai",
             lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o")),
            ("Gemini",            "gemini",
             lambda: _chamar_gemini(system, user, max_tokens)),
        ]
    elif complexidade == Complexidade.MEDIA:
        candidatos = [
            ("GPT-4o mini",      "openai",
             lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o-mini")),
            ("Claude Haiku 4.5", "claude",
             lambda: _chamar_claude(system, user, min(max_tokens, 4096))),
            ("Gemini",           "gemini",
             lambda: _chamar_gemini(system, user, max_tokens)),
        ]
    else:  # BAIXA — chatbox / assist / CARE: GPT-4o mini primário, Claude Haiku fallback, Gemini opcional.
        candidatos = [
            ("GPT-4o mini",      "openai",
             lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o-mini")),
            ("Claude Haiku 3.5", "claude",
             lambda: _chamar_claude(system, user, min(max_tokens, 2048))),
            ("Gemini",           "gemini",
             lambda: _chamar_gemini(system, user, max_tokens)),
        ]

    # Filtra apenas providers com chave disponível
    cadeia = [(nome, fn) for nome, prov, fn in candidatos if _tem_chave(prov)]

    # Garantia final: se nada disponível, avisa claramente
    if not cadeia:
        raise RuntimeError(
            "Nenhum provider LLM disponível. "
            "Configure ANTHROPIC_API_KEY ou OPENAI_API_KEY no .env / Render."
        )

    # Timeout por complexidade: artigo completo pode levar 3-4 min no Claude
    timeouts = {
        Complexidade.ALTA:  360,  # 6 min — geração de artigo completo
        Complexidade.MEDIA: 120,  # 2 min — revisão, assist-texto
        Complexidade.BAIXA:  60,  # 1 min — chatbox
    }
    timeout = timeouts[complexidade]

    erros = []
    for nome, fn in cadeia:
        try:
            logger.info(f"LLM → {nome} (complexidade={complexidade}, timeout={timeout}s)")
            return await asyncio.wait_for(fn(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"  ✗ {nome} timeout após {timeout}s")
            erros.append(f"{nome}: timeout")
        except Exception as e:
            logger.warning(f"  ✗ {nome} falhou: {e}")
            erros.append(f"{nome}: {e}")

    raise RuntimeError("Todos os providers falharam. " + " | ".join(erros))


# ── Helper ────────────────────────────────────────────────────────────────────

def extrair_json(texto: str) -> dict:
    raw = texto.strip()
    # Remove blocos markdown ```json ... ```
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    # Tenta parse direto
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # JSON truncado — tenta reparar fechando chaves/colchetes abertos
    try:
        import re
        # Encontra o último { ou [ aberto sem fechar
        depth_curly = depth_square = 0
        in_string = escaped = False
        for ch in raw:
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_string:
                escaped = True
                continue
            if ch == '"' and not escaped:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth_curly += 1
            elif ch == '}':
                depth_curly -= 1
            elif ch == '[':
                depth_square += 1
            elif ch == ']':
                depth_square -= 1

        # Fecha string aberta se necessário
        if in_string:
            raw += '"'
        # Fecha estruturas abertas
        raw += ']' * max(0, depth_square) + '}' * max(0, depth_curly)

        result = json.loads(raw)
        logger.warning("extrair_json: JSON truncado reparado automaticamente")
        return result
    except Exception as e:
        raise ValueError(f"Não foi possível extrair JSON válido da resposta LLM: {e}\nResposta (primeiros 300 chars): {texto[:300]}")
