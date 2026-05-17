"""
NLM MeSH API — valida descritores MeSH gerados pelo LLM.
Sem chave. API pública do National Library of Medicine.
Uso: verificar se palavras-chave do artigo são termos MeSH reais.
"""
import asyncio
from typing import Union
import httpx

_BASE = "https://id.nlm.nih.gov/mesh"
_RATE = asyncio.Semaphore(3)


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> Union[list, dict]:
    async with _RATE:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        await asyncio.sleep(0.4)
        return resp.json()


async def validar_termo(termo: str) -> dict:
    """
    Verifica se um termo é descritor MeSH válido.
    Tenta correspondência exata, depois parcial.
    Retorna: {valido, termo_original, termo_mesh, ui, tipo}
    """
    termo = termo.strip()
    async with httpx.AsyncClient() as client:
        # 1. Busca exata
        try:
            data = await _get(client, f"{_BASE}/lookup/term",
                               {"label": termo, "match": "exact", "limit": 1})
            if data:
                item = data[0]
                return {
                    "valido": True,
                    "termo_original": termo,
                    "termo_mesh": item.get("label", termo),
                    "ui": item.get("ui", ""),
                    "tipo": "exato",
                }
        except Exception:
            pass

        # 2. Busca contém
        try:
            data = await _get(client, f"{_BASE}/lookup/term",
                               {"label": termo, "match": "contains", "limit": 3})
            if data:
                item = data[0]
                return {
                    "valido": True,
                    "termo_original": termo,
                    "termo_mesh": item.get("label", termo),
                    "ui": item.get("ui", ""),
                    "tipo": "aproximado",
                }
        except Exception:
            pass

    return {"valido": False, "termo_original": termo, "termo_mesh": None, "ui": None}


async def validar_lista(termos: list[str]) -> list[dict]:
    """Valida múltiplos termos MeSH e retorna lista com status de cada um."""
    sem = asyncio.Semaphore(3)

    async def _um(t: str) -> dict:
        async with sem:
            return await validar_termo(t)

    return await asyncio.gather(*[_um(t) for t in termos])


async def sugerir_descritor(termo_livre: str) -> list[str]:
    """Sugere descritores MeSH a partir de texto livre (busca parcial)."""
    async with httpx.AsyncClient() as client:
        try:
            data = await _get(client, f"{_BASE}/lookup/term",
                               {"label": termo_livre, "match": "startswith", "limit": 5})
            return [item.get("label", "") for item in data if item.get("label")]
        except Exception:
            return []


async def validar_e_corrigir_palavras_chave(palavras: list[str]) -> dict:
    """
    Valida lista de palavras-chave geradas pelo LLM.
    Retorna: {validadas, invalidas, sugestoes, score}
    """
    resultados = await validar_lista(palavras)
    validadas, invalidas, sugestoes = [], [], []

    for res in resultados:
        if res["valido"]:
            validadas.append(res["termo_mesh"] or res["termo_original"])
        else:
            invalidas.append(res["termo_original"])
            sugs = await sugerir_descritor(res["termo_original"])
            if sugs:
                sugestoes.append({"original": res["termo_original"], "sugeridos": sugs[:3]})

    score = len(validadas) / len(palavras) if palavras else 0

    return {
        "validadas": validadas,
        "invalidas": invalidas,
        "sugestoes": sugestoes,
        "score_mesh": round(score, 2),
    }
