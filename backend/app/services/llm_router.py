"""
LLM Router — roteamento por complexidade com fallback em cascata.

Hierarquia de providers:
  ALTA complexidade  → Claude Sonnet 4.6  → GPT-4o         → Gemini 2.0 Flash
  MÉDIA complexidade → GPT-4o mini        → Gemini 2.0 Flash → Claude Haiku
  BAIXA complexidade → Gemini 2.0 Flash   → GPT-4o mini    → Claude Haiku

Custo estimado por artigo:
  Claude Sonnet 4.6  ~$0.35
  GPT-4o mini        ~$0.02
  Gemini 2.0 Flash   ~$0.001 (fallback — quase gratuito)
  Total              ~$0.37  ≈ R$ 2,00
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


# ── Clientes ──────────────────────────────────────────────────────────────────

def _claude() -> anthropic.AsyncAnthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada.")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _openai_async() -> openai.AsyncOpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada.")
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)


def _gemini_model(model: str = "gemini-2.0-flash"):
    if not settings.gemini_api_key:
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
    if complexidade == Complexidade.ALTA:
        cadeia = [
            ("Claude Sonnet 4.6",  lambda: _chamar_claude(system, user, max_tokens)),
            ("GPT-4o",             lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o")),
            ("Gemini 1.5 Pro",     lambda: _chamar_gemini(system, user, "gemini-2.0-flash")),
        ]
    elif complexidade == Complexidade.MEDIA:
        cadeia = [
            ("GPT-4o mini",        lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o-mini")),
            ("Gemini 1.5 Flash",   lambda: _chamar_gemini(system, user, "gemini-2.0-flash")),
            ("Claude Haiku 4.5",   lambda: _chamar_claude(system, user, min(max_tokens, 4096))),
        ]
    else:
        cadeia = [
            ("Gemini 1.5 Flash",   lambda: _chamar_gemini(system, user, "gemini-2.0-flash")),
            ("GPT-4o mini",        lambda: _chamar_openai(system, user, max_tokens, json_mode, "gpt-4o-mini")),
            ("Claude Haiku 4.5",   lambda: _chamar_claude(system, user, min(max_tokens, 2048))),
        ]

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
