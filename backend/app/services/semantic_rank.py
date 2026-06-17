"""
Semantic Ranking — re-rankeia artigos por relevância semântica ao caso clínico.

Usa GPT-4o mini (MEDIA, ~$0.001 por chamada) para comparar títulos/abstracts
contra o contexto do caso. Retorna os mesmos artigos com dois campos extras:
  • relevancia_semantica: int (1-10)
  • semantico_top: bool  (True nos top-5)

Projetado para ser não-bloqueante: falhas retornam artigos com score=0 e
a ordenação original é preservada.
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ARTIGOS = 20        # máximo que mandamos para o LLM de uma vez
_TOP_N       = 5         # quantos recebem o badge "⭐ Mais relevante"

_SYSTEM = (
    "Você é especialista em revisão bibliográfica médica. "
    "Avalie numericamente (1-10) a relevância de cada artigo para o caso clínico descrito. "
    "10 = essencial para embasar o relato; 1 = irrelevante. "
    "Responda APENAS com JSON: {\"rankings\": [{\"i\": 0, \"s\": 8}, ...]}"
)


def _resumo_artigo(art: dict, idx: int) -> str:
    titulo  = (art.get("titulo") or "")[:90]
    abstrac = (art.get("abstract") or "")[:100]
    ano     = art.get("ano") or ""
    return f"{idx}. {titulo} ({ano}) | {abstrac}"


async def ranquear(
    artigos: list[dict[str, Any]],
    contexto: str,
) -> list[dict[str, Any]]:
    """
    Recebe lista de artigos e um contexto (diagnóstico + intervenção + HDA snippet).
    Retorna os MESMOS artigos (mesma ordem) com os campos extras adicionados.

    Se o LLM falhar, retorna os artigos com relevancia_semantica=0 e semantico_top=False.
    """
    if not artigos:
        return artigos

    # Limita para não explodir o contexto do LLM
    pool = artigos[:_MAX_ARTIGOS]
    for art in artigos:
        art.setdefault("relevancia_semantica", 0)
        art.setdefault("semantico_top", False)

    linhas = "\n".join(_resumo_artigo(art, i) for i, art in enumerate(pool))
    user_prompt = (
        f"CASO CLÍNICO:\n{contexto[:500]}\n\n"
        f"ARTIGOS (índice. título (ano) | abstract):\n{linhas}\n\n"
        "Retorne JSON com os índices e scores de relevância."
    )

    try:
        from app.services.llm_router import chamar, extrair_json, Complexidade
        resp = await chamar(
            _SYSTEM, user_prompt,
            complexidade=Complexidade.MEDIA,
            max_tokens=400,
            json_mode=True,
        )
        data = extrair_json(resp)
        rankings: list[dict] = data.get("rankings", [])

        # Aplica scores ao pool
        score_map: dict[int, int] = {}
        for entry in rankings:
            idx = entry.get("i")
            s   = entry.get("s", 0)
            if isinstance(idx, int) and 0 <= idx < len(pool):
                score_map[idx] = int(s)

        for i, art in enumerate(pool):
            art["relevancia_semantica"] = score_map.get(i, 0)

        # Marca top-N como semantico_top
        indices_sorted = sorted(range(len(pool)), key=lambda x: pool[x]["relevancia_semantica"], reverse=True)
        for rank_pos, art_idx in enumerate(indices_sorted):
            pool[art_idx]["semantico_top"] = (rank_pos < _TOP_N)

        logger.info("Semantic ranking concluído: %d artigos avaliados, top-%d marcados", len(pool), _TOP_N)

    except Exception as exc:
        logger.warning("Semantic ranking falhou (artigos sem score): %s", exc)
        # Deixa os artigos como estão — score=0, semantico_top=False já setados acima

    return artigos   # inclui artigos além do _MAX_ARTIGOS (não alterados)


def ordenar_por_relevancia(artigos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Reordena artigos colocando os semantico_top primeiro,
    depois por relevancia_semantica desc, depois por citações desc.
    Útil para apresentar resultados no dashboard.
    """
    return sorted(
        artigos,
        key=lambda a: (
            -int(a.get("semantico_top") or 0),
            -(a.get("relevancia_semantica") or 0),
            -(a.get("citation_count") or a.get("citacoes") or 0),
        ),
    )
