"""
LLM Router — roteamento por complexidade com fallback em cascata.

Hierarquia definida (aplicada apenas se a chave estiver configurada):
  ALTA complexidade  → Claude Sonnet 4.6  → GPT-4o         → Gemini 2.0 Flash
  MÉDIA complexidade → GPT-4o mini        → Gemini 2.0 Flash → Claude Haiku
  BAIXA complexidade → Gemini 2.0 Flash   → GPT-4o mini    → Claude Haiku

Regra de disponibilidade:
  • Providers sem chave configurada são automaticamente ignorados.
  • Gemini é o fallback universal — se for o único com chave, é sempre usado.
  • Quando Claude/OpenAI forem contratados, basta adicionar as chaves ao .env
    e a hierarquia entra em vigor automaticamente, sem alterar código.

Custo estimado por artigo (quando todos disponíveis):
  Claude Sonnet 4.6  ~$0.35
  GPT-4o mini        ~$0.02
  Gemini 2.0 Flash   ~$0.001
"""
import json
import logging
from enum import Enum
from typing import Any

import anthropic
import openai
import google.generativeai as genai

from app.config import settings

logger = logging.getLogger(__name__)


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


# ── Clientes ──────────────────────────────────────────────────────────────────

def _claude() -> anthropic.AsyncAnthropic:
    if not _tem_chave("claude"):
        raise RuntimeError("ANTHROPIC_API_KEY não configurada.")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _openai_async() -> openai.AsyncOpenAI:
    if not _tem_chave("openai"):
        raise RuntimeError("OPENAI_API_KEY não configurada.")
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)


def _gemini_model(model: str = "gemini-2.0-flash") -> genai.GenerativeModel:
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
    return resp.choices[0].message.content.strip()


async def _chamar_gemini(
    system: str, user: str, model: str = "gemini-2.0-flash"
) -> str:
    m = _gemini_model(model)
    prompt = f"{system}\n\n{user}"
    resp = await m.generate_content_async(prompt)
    return resp.text.strip()


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
    Gemini é o fallback universal — sempre incluído se tiver chave.
    """
    # Cada item: (nome_display, provider_key, callable)
    if complexidade == Complexidade.ALTA:
        candidatos = [
            ("Claude Sonnet 4.6", "claude",
             lambda: _chamar_claude(system, user, max_tokens)),
            ("GPT-4o",            "openai",
             lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o")),
            ("Gemini 2.0 Flash",  "gemini",
             lambda: _chamar_gemini(system, user)),
        ]
    elif complexidade == Complexidade.MEDIA:
        candidatos = [
            ("GPT-4o mini",       "openai",
             lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o-mini")),
            ("Gemini 2.0 Flash",  "gemini",
             lambda: _chamar_gemini(system, user)),
            ("Claude Haiku 4.5",  "claude",
             lambda: _chamar_claude(system, user, min(max_tokens, 4096))),
        ]
    else:  # BAIXA — Gemini primeiro (padrão quando apenas Gemini está disponível)
        candidatos = [
            ("Gemini 2.0 Flash",  "gemini",
             lambda: _chamar_gemini(system, user)),
            ("GPT-4o mini",       "openai",
             lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o-mini")),
            ("Claude Haiku 4.5",  "claude",
             lambda: _chamar_claude(system, user, min(max_tokens, 2048))),
        ]

    # Filtra apenas providers com chave disponível
    cadeia = [(nome, fn) for nome, prov, fn in candidatos if _tem_chave(prov)]

    # Garantia final: se nada disponível, avisa claramente
    if not cadeia:
        raise RuntimeError(
            "Nenhum provider LLM disponível. "
            "Configure pelo menos GEMINI_API_KEY no .env."
        )

    ultimo_erro = None
    for nome, fn in cadeia:
        try:
            logger.info(f"LLM → {nome} (complexidade={complexidade})")
            return await fn()
        except Exception as e:
            logger.warning(f"  ✗ {nome} falhou: {e}")
            ultimo_erro = e

    raise RuntimeError(f"Todos os providers falharam. Último erro: {ultimo_erro}")


# ── Helper ────────────────────────────────────────────────────────────────────

def extrair_json(texto: str) -> dict:
    raw = texto.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)
