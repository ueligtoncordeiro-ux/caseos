import os
import asyncio
import copy
import io
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional, Any
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.models.schemas import CKO, IniciarResponse, StatusResponse
from app.models.database import PesquisaSalva, Sessao, VersaoDocx, get_db, Usuario
from app.agents.orchestrator import executar_pipeline, executar_pipeline_demo
from app.services.auth import get_verified_user, check_quota
from app.services.scientific_search import buscar_literatura

router = APIRouter(prefix="/artigo", tags=["artigo"])
_log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS COMPARTILHADOS — usados por download e editar_secao
# ══════════════════════════════════════════════════════════════════════════════

def _to_str(v) -> str:
    """Converte qualquer valor para string de forma segura."""
    if v is None: return ""
    if isinstance(v, str): return v
    if isinstance(v, dict): return str(v.get("formatada") or v.get("texto") or "")
    return str(v)


def _to_str_list(v) -> list:
    """Garante list[str] independente do tipo armazenado."""
    if v is None: return []
    if isinstance(v, str): return [v] if v.strip() else []
    if isinstance(v, list): return [_to_str(i) for i in v if i not in (None, "")]
    return [str(v)]


def _normalizar_resultado(raw: dict, sessao_id: str) -> dict:
    """
    Normaliza o dict resultado para que passe em ArtigoGerado(**res)
    independente do estado (recém gerado, parcialmente editado, importado).
    """
    res = copy.deepcopy(raw)

    # ── Renomear campos legados ANTES de qualquer remoção de chaves ──────────
    # O pipeline antigo usava "relato_caso"; o schema atual usa "caso_clinico"
    if "caso_clinico" not in res and "relato_caso" in res:
        res["caso_clinico"] = res.pop("relato_caso")

    SCHEMA_KEYS = {"titulo","palavras_chave","resumo","introducao",
                   "caso_clinico","discussao","conclusao","referencias"}
    for k in list(res.keys()):
        if k not in SCHEMA_KEYS:
            res.pop(k, None)

    res["titulo"] = _to_str(res.get("titulo")) or "Relato de Caso"

    pck = res.get("palavras_chave", [])
    if isinstance(pck, str):
        res["palavras_chave"] = [p.strip() for p in pck.replace(";",",").split(",") if p.strip()]
    else:
        res["palavras_chave"] = [_to_str(p) for p in (pck or []) if p]

    for campo in ("introducao", "caso_clinico", "discussao", "conclusao"):
        res[campo] = _to_str_list(res.get(campo))

    rs_raw = res.get("resumo") or {}
    if not isinstance(rs_raw, dict):
        rs_raw = {}
    if "caso" not in rs_raw and "relato" in rs_raw:
        rs_raw["caso"] = rs_raw.pop("relato")
    for campo_rs in ("introducao", "caso", "discussao", "conclusao"):
        rs_raw[campo_rs] = _to_str(rs_raw.get(campo_rs))
    res["resumo"] = rs_raw

    refs_raw = res.get("referencias") or []
    refs_norm = []
    for i, ref in enumerate(refs_raw, start=1):
        if isinstance(ref, str):
            # Referência salva como string pura
            refs_norm.append({
                "numero": i, "autores": "", "titulo": ref,
                "periodico": "", "ano": "", "formatada": ref,
            })
        elif isinstance(ref, dict):
            # ⚠️ Construção explícita — não usar setdefault, pois versões antigas
            # do servidor podem não ter commitado as normalizações anteriores.
            formatada = _to_str(ref.get("formatada") or ref.get("titulo", ""))
            refs_norm.append({
                "numero":   ref.get("numero") or i,
                "autores":  _to_str(ref.get("autores")),
                "titulo":   _to_str(ref.get("titulo") or formatada),
                "periodico":_to_str(ref.get("periodico")),
                "ano":      _to_str(ref.get("ano")),
                "formatada":formatada,
                # campos opcionais — preservar se existirem
                **({"volume":       ref["volume"]}       if "volume"       in ref else {}),
                **({"numero_edicao":ref["numero_edicao"]} if "numero_edicao" in ref else {}),
                **({"paginas":      ref["paginas"]}      if "paginas"      in ref else {}),
                **({"doi":          ref["doi"]}          if "doi"          in ref else {}),
                **({"pmid":         ref["pmid"]}         if "pmid"         in ref else {}),
            })
    res["referencias"] = refs_norm
    return res


def _cko_fallback(sessao_id: str, cko_data: dict):
    """Tenta parsear CKO, retorna mínimo válido se falhar."""
    from app.models.schemas import (
        CKO as CKOSchema, Identificacao, Historia, Achados, Diagnostico,
        Intervencao, Desfechos, Editorial, IntervencoesAnteriores, Timeline,
    )
    try:
        return CKOSchema(**cko_data)
    except Exception:
        return CKOSchema(
            sessao_id=sessao_id,
            identificacao=Identificacao(),
            historia=Historia(queixa_principal="", hda=""),
            intervencoes_anteriores=IntervencoesAnteriores(),
            achados=Achados(exame_geral="", achados_especificos=""),
            timeline=Timeline(),
            diagnostico=Diagnostico(diagnostico_definitivo=""),
            intervencao=Intervencao(tipo="", descricao=""),
            desfechos=Desfechos(desfecho_clinico=""),
            editorial=Editorial(problemas_clinicos="", diferencial_caso=""),
        )


async def _gerar_bytes_do_resultado(sessao: Sessao) -> bytes:
    """
    Normaliza sessao.resultado e gera os bytes do DOCX.
    Lança exceção descritiva se falhar em qualquer etapa.
    """
    from app.models.schemas import ArtigoGerado
    from app.services.docx_generator import gerar_docx_bytes

    if not sessao.resultado:
        raise ValueError("sessao.resultado está vazio")

    res = _normalizar_resultado(sessao.resultado, sessao.external_id)
    try:
        artigo = ArtigoGerado(**res)
    except Exception as e:
        raise ValueError(f"Estrutura do artigo inválida: {e}") from e

    cko = _cko_fallback(sessao.external_id, sessao.cko or {})
    return await gerar_docx_bytes(artigo, cko, sessao.external_id)

# ── Rate limiting simples para /demo (sem dependência externa) ────────────────
_DEMO_MAX_POR_HORA = 3          # máximo de demos por IP por hora
_DEMO_JANELA_SEG   = 3600       # janela de 1 hora
_demo_hits: dict[str, list[float]] = defaultdict(list)   # ip → [timestamps]


def _check_demo_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    agora = time.time()
    janela_inicio = agora - _DEMO_JANELA_SEG

    # Remove timestamps fora da janela
    _demo_hits[ip] = [t for t in _demo_hits[ip] if t > janela_inicio]

    if len(_demo_hits[ip]) >= _DEMO_MAX_POR_HORA:
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {_DEMO_MAX_POR_HORA} demos por hora atingido. "
                   "Crie uma conta gratuita para continuar.",
        )
    _demo_hits[ip].append(agora)


class SalvarPesquisaRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=300)
    operador: str = "AND"
    fontes: list[str] = Field(default_factory=list)
    artigos: list[dict[str, Any]] = Field(default_factory=list)
    tipo: str = Field(default="busca", pattern="^(busca|artigo|caso_clinico)$")
    observacao: Optional[str] = Field(default=None, max_length=500)


@router.post("/demo", response_model=IniciarResponse)
async def iniciar_demo(
    cko: CKO,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Pipeline de demonstração — sem autenticação. Gera apenas Introdução + Caso Clínico.
    Limitado a 3 demos/hora por IP para evitar abuso de LLM."""
    _check_demo_rate_limit(request)
    background_tasks.add_task(executar_pipeline_demo, cko.sessao_id, cko)

    return IniciarResponse(
        sessao_id=cko.sessao_id,
        status="iniciado",
        mensagem="Pipeline de demonstração iniciado. Acompanhe via WebSocket.",
    )


@router.post("/iniciar", response_model=IniciarResponse)
async def iniciar_geracao(
    cko: CKO,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(check_quota),
):
    result = await db.execute(
        select(Sessao).where(Sessao.external_id == cko.sessao_id)
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Permite apenas se a sessão ainda não tem dono (prévia demo → upgrade para relato)
        # ou se o dono é o próprio usuário.
        # O `and` intencional: NULL significa sessão anônima de demo, que pode ser reivindicada.
        if existing.user_id is not None and existing.user_id != user.id:
            raise HTTPException(status_code=403, detail="Sessão pertence a outro usuário.")
        if existing.status in ("gerando", "redigindo", "revisando", "finalizando"):
            raise HTTPException(status_code=409, detail="Sessão já está sendo processada.")
        if existing.status == "concluido":
            raise HTTPException(status_code=409,
                                detail="Sessão já concluída. Inicie um novo relato.")
        existing.status  = "rascunho"
        existing.cko     = cko.model_dump()
        existing.user_id = user.id
        await db.commit()
    else:
        # Título automático a partir da queixa principal
        queixa = cko.historia.queixa_principal or ""
        titulo_auto = queixa[:80] if queixa else f"Relato {cko.sessao_id[:8]}"
        sessao = Sessao(
            external_id=cko.sessao_id,
            status="rascunho",
            cko=cko.model_dump(),
            user_id=user.id,
            titulo=titulo_auto,
        )
        db.add(sessao)
        await db.commit()

    background_tasks.add_task(executar_pipeline, cko.sessao_id, cko, user.id, user.nome)

    return IniciarResponse(
        sessao_id=cko.sessao_id,
        status="iniciado",
        mensagem="Pipeline iniciado. Acompanhe via WebSocket.",
    )


@router.get("/historico")
async def historico(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=10, ge=1, le=50),
    status: Optional[str] = Query(default=None),
):
    """Lista os relatos do usuário com paginação."""
    q = select(Sessao).where(Sessao.user_id == user.id)
    if status:
        q = q.where(Sessao.status == status)
    q = q.order_by(desc(Sessao.created_at))

    total_q = select(func.count()).select_from(
        select(Sessao).where(Sessao.user_id == user.id).subquery()
    )
    total_r = await db.execute(total_q)
    total = total_r.scalar() or 0

    q = q.offset((pagina - 1) * por_pagina).limit(por_pagina)
    result = await db.execute(q)
    sessoes = result.scalars().all()

    total_paginas = max(1, (total + por_pagina - 1) // por_pagina)
    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "total_paginas": total_paginas,   # campo que o dashboard lê
        "paginas": total_paginas,          # alias de compatibilidade
        "itens": [                         # campo que o dashboard lê
            {
                "sessao_id": s.external_id,
                "titulo": s.titulo or f"Relato {s.external_id[:8]}",
                "status": s.status,
                "care_score": s.care_score,
                "criado_em": s.created_at.isoformat() if s.created_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                "tem_docx": bool(s.docx_path and Path(s.docx_path).exists()),
                "versao_edicao": (s.resultado or {}).get("versao_edicao", 1) if s.resultado else 1,
            }
            for s in sessoes
        ],
    }


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Estatísticas do usuário para o dashboard."""
    result = await db.execute(
        select(Sessao).where(Sessao.user_id == user.id)
    )
    sessoes = result.scalars().all()

    total = len(sessoes)
    concluidos = [s for s in sessoes if s.status == "concluido"]
    em_andamento = [s for s in sessoes if s.status in ("gerando", "redigindo", "revisando", "finalizando")]
    com_erro = [s for s in sessoes if s.status == "erro"]

    scores = [s.care_score for s in concluidos if s.care_score is not None]
    media_care = round(sum(scores) / len(scores), 1) if scores else None

    return {
        "total_relatos": total,
        "concluidos": len(concluidos),
        "em_andamento": len(em_andamento),
        "com_erro": len(com_erro),
        "media_care_score": media_care,
        "artigos_mes": user.artigos_mes,
        "plano": user.plano,
    }


@router.patch("/{sessao_id}")
async def atualizar_sessao(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Atualiza título do relato."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    if "titulo" in body:
        novo_titulo = str(body["titulo"])[:120]
        sessao.titulo = novo_titulo

        # Sincroniza resultado["titulo"] para que o DOCX reflita a alteração
        if sessao.resultado:
            from sqlalchemy.orm.attributes import flag_modified
            resultado = dict(sessao.resultado)
            resultado["titulo"] = novo_titulo
            sessao.resultado = resultado
            flag_modified(sessao, "resultado")

            # Regenera docx_editado_bytes para que "+ Nova Versão" pegue o título novo
            try:
                sessao.docx_editado_bytes = await _gerar_bytes_do_resultado(sessao)
            except Exception as e:
                _log.warning("Falha ao regenerar DOCX após edição de título: %s", e)

    await db.commit()
    return {"sessao_id": sessao_id, "titulo": sessao.titulo}


@router.delete("/{sessao_id}")
async def deletar_sessao(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Remove um relato do histórico (e arquivo DOCX se existir)."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    # Remove arquivo DOCX se existir
    if sessao.docx_path and Path(sessao.docx_path).exists():
        try:
            os.remove(sessao.docx_path)
        except OSError:
            pass

    await db.delete(sessao)
    await db.commit()
    return {"deletado": True, "sessao_id": sessao_id}


@router.get("/{sessao_id}/status", response_model=StatusResponse)
async def status_sessao(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    # Verificação estrita: sessão sem dono (demo não reivindicada) também é negada.
    # Só o dono pode consultar o status de um relato.
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    return StatusResponse(
        sessao_id=sessao_id,
        status=sessao.status,
        resultado=sessao.relatorio if sessao.status == "concluido" else None,
    )


@router.get("/{sessao_id}")
async def detalhe_sessao(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Retorna dados completos da sessão (cko + resultado + relatorio) para visualização."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    return {
        "sessao_id": sessao.external_id,
        "titulo": sessao.titulo or f"Relato {sessao.external_id[:8]}",
        "status": sessao.status,
        "care_score": sessao.care_score,
        "criado_em": sessao.created_at.isoformat() if sessao.created_at else None,
        "cko": sessao.cko,
        "resultado": sessao.resultado,
        "relatorio": sessao.relatorio,
        "flags": sessao.flags,
        "tokens_usados": sessao.tokens_usados,
        # ── Versões DOCX ──────────────────────────────────────────────────────
        "docx_original_gerado": sessao.docx_original_bytes is not None,
        "tem_versao_editada":   sessao.docx_editado_bytes is not None,
    }


@router.get("/{sessao_id}/resultado")
async def download_resultado(
    sessao_id: str,
    versao: str = Query("editado", description="'editado' (padrão) ou 'original'"),
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """
    Entrega o DOCX como StreamingResponse.
    Serve direto dos bytes armazenados no banco (gerados pelo pipeline ou ao salvar edições).
    Fallback: regenera do JSON para sessões antigas sem bytes no banco.
    """
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()

    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if sessao.status != "concluido":
        raise HTTPException(status_code=425,
                            detail=f"Artigo ainda não concluído. Status: {sessao.status}")

    try:
        # ── 1. Tenta servir diretamente dos bytes armazenados ──────────────────
        if versao == "original":
            docx_bytes = sessao.docx_original_bytes
            sufixo = "_original"
        else:
            # editado → prefere editado, cai para original se não houver edição
            docx_bytes = sessao.docx_editado_bytes or sessao.docx_original_bytes
            sufixo = "_editado" if sessao.docx_editado_bytes else ""

        # ── 2. Fallback: sessões antigas sem bytes no banco → regenera do JSON ─
        if not docx_bytes:
            if not sessao.resultado:
                raise HTTPException(status_code=404,
                                    detail="Conteúdo do artigo não encontrado.")
            _log.warning("Fallback JSON→DOCX para sessão %s (sem bytes no banco)", sessao_id)
            docx_bytes = await _gerar_bytes_do_resultado(sessao)
            # Persiste como original para downloads futuros serem instantâneos
            sessao.docx_original_bytes = docx_bytes
            await db.commit()

        _log.info("Download DOCX sessão=%s versao=%s bytes=%d", sessao_id, versao, len(docx_bytes))
        filename = f"CaseOS_{sessao_id[:8]}{sufixo}.docx"
        return StreamingResponse(
            content=io.BytesIO(docx_bytes),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        _log.error("Erro ao gerar DOCX sessão %s:\n%s", sessao_id, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Erro ao gerar DOCX: {exc}")


@router.get("/{sessao_id}/diagnostico")
async def diagnostico_resultado(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """
    Endpoint de diagnóstico (owner-only).
    Retorna o raw resultado + o que o parser consegue extrair + qualquer erro.
    Útil para depurar falhas de download sem precisar de acesso ao servidor.
    """
    import copy
    from app.models.schemas import ArtigoGerado

    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao or sessao.user_id != user.id:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")

    raw = sessao.resultado or {}
    erros = []
    parsed_ok = False

    # tenta parsear diretamente sem normalização
    try:
        from app.models.schemas import ArtigoGerado
        ArtigoGerado(**copy.deepcopy(raw))
        parsed_ok = True
    except Exception as e:
        erros.append(f"parse_direto: {e}")

    return {
        "sessao_id": sessao_id,
        "status": sessao.status,
        "parsed_sem_normalizacao": parsed_ok,
        "erros": erros,
        "chaves_resultado": list(raw.keys()) if raw else [],
        "tipos_campos": {
            k: type(v).__name__ for k, v in raw.items()
        } if raw else {},
        "resumo_chaves": list(raw.get("resumo", {}).keys()) if isinstance(raw.get("resumo"), dict) else str(type(raw.get("resumo"))),
        "referencias_count": len(raw.get("referencias", [])),
        "referencias_sample": (raw.get("referencias") or [])[:2],
        "palavras_chave_tipo": type(raw.get("palavras_chave")).__name__,
    }


# ── Versões DOCX ──────────────────────────────────────────────────────────────

@router.post("/{sessao_id}/versao")
async def criar_versao(
    sessao_id: str,
    body: dict = {},
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """
    Cria uma nova versão DOCX a partir do estado atual do artigo.
    Cada versão é imutável e fica disponível para download posterior.
    """
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if sessao.status != "concluido":
        raise HTTPException(status_code=425, detail="Artigo ainda não concluído.")

    # ── Gera o DOCX sempre do sessao.resultado (fonte de verdade) ───────────────
    # sessao.resultado é atualizado por TODA edição: seções (editar_secao) E
    # título (atualizar_sessao). Regenerar garante que a versão reflita EXATAMENTE
    # o estado atual — sem depender de docx_editado_bytes que pode ser de uma
    # edição anterior e não incluir o último campo alterado (ex.: título).
    # Fallback para bytes armazenados apenas em caso de falha na geração.
    fonte = "regenerado"
    try:
        docx_bytes = await _gerar_bytes_do_resultado(sessao)
    except Exception as gen_err:
        _log.warning("Falha ao regenerar DOCX para versão (sessao=%s): %s — usando fallback", sessao_id, gen_err)
        if sessao.docx_editado_bytes:
            docx_bytes = sessao.docx_editado_bytes
            fonte = "editado_fallback"
        elif sessao.docx_original_bytes:
            docx_bytes = sessao.docx_original_bytes
            fonte = "original_fallback"
        else:
            raise HTTPException(status_code=500, detail=f"Erro ao gerar DOCX: {gen_err}")

    _log.info("Versão DOCX fonte=%s sessao=%s bytes=%d", fonte, sessao_id, len(docx_bytes))

    # Descobre o próximo número de versão
    from sqlalchemy import func as sqlfunc
    count_q = await db.execute(
        select(sqlfunc.count()).select_from(VersaoDocx).where(
            VersaoDocx.sessao_external_id == sessao_id
        )
    )
    total_versoes = count_q.scalar() or 0
    numero = total_versoes + 1

    descricao = str(body.get("descricao", "")).strip()[:200] or None

    nova_versao = VersaoDocx(
        sessao_external_id=sessao_id,
        numero=numero,
        docx_bytes=docx_bytes,
        descricao=descricao,
    )
    db.add(nova_versao)
    await db.commit()
    await db.refresh(nova_versao)

    _log.info("Nova versão DOCX criada: sessao=%s versao=%d bytes=%d",
              sessao_id, numero, len(docx_bytes))

    return {
        "id": nova_versao.id,
        "numero": nova_versao.numero,
        "descricao": nova_versao.descricao,
        "created_at": nova_versao.created_at.isoformat(),
        "size_kb": round(len(docx_bytes) / 1024, 1),
    }


@router.get("/{sessao_id}/versoes")
async def listar_versoes(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Lista todas as versões DOCX salvas pelo usuário para este relato."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    versoes_q = await db.execute(
        select(VersaoDocx)
        .where(VersaoDocx.sessao_external_id == sessao_id)
        .order_by(VersaoDocx.numero)
    )
    versoes = versoes_q.scalars().all()

    return {
        "total": len(versoes),
        "versoes": [
            {
                "id": v.id,
                "numero": v.numero,
                "descricao": v.descricao,
                "created_at": v.created_at.isoformat(),
                "size_kb": round(len(v.docx_bytes) / 1024, 1) if v.docx_bytes else 0,
            }
            for v in versoes
        ],
    }


@router.get("/{sessao_id}/versao/{versao_id}/resultado")
async def download_versao(
    sessao_id: str,
    versao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Download de uma versão DOCX específica."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")

    versao_q = await db.execute(
        select(VersaoDocx).where(
            VersaoDocx.id == versao_id,
            VersaoDocx.sessao_external_id == sessao_id,
        )
    )
    versao = versao_q.scalar_one_or_none()
    if not versao:
        raise HTTPException(status_code=404, detail="Versão não encontrada.")

    filename = f"CaseOS_{sessao_id[:8]}_v{versao.numero}.docx"
    return StreamingResponse(
        content=io.BytesIO(versao.docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{sessao_id}/confirmar-flags")
async def confirmar_flags(
    sessao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    return {"sessao_id": sessao_id, "confirmado": True}


@router.get("/pubmed/buscar")
async def buscar_pubmed(
    q: str = Query(..., min_length=3, max_length=200),
    max: int = Query(default=10, ge=1, le=20),
    user: Usuario = Depends(get_verified_user),
):
    """Busca artigos no PubMed e retorna lista com links diretos."""
    from app.utils.pubmed import pesquisar_pubmed

    try:
        artigos = await pesquisar_pubmed([q], max_por_query=max, max_total=max)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro na busca PubMed: {str(exc)[:200]}")

    def _pubmed_url(art: dict) -> str:
        if art.get("pmid"):
            return f"https://pubmed.ncbi.nlm.nih.gov/{art['pmid']}"
        if art.get("doi"):
            return f"https://doi.org/{art['doi']}"
        return ""

    return {
        "query": q,
        "total": len(artigos),
        "artigos": [
            {
                "pmid":       art.get("pmid", ""),
                "titulo":     art.get("titulo", "Sem título"),
                "autores":    art.get("autores", ""),
                "periodico":  art.get("periodico", ""),
                "ano":        art.get("ano", ""),
                "abstract":   (art.get("abstract") or "")[:500],
                "doi":        art.get("doi", ""),
                "url_pubmed": _pubmed_url(art),
                "citacoes":   art.get("citation_count") or art.get("citacoes", 0),
            }
            for art in artigos
        ],
    }


@router.get("/biblioteca/buscar")
async def buscar_biblioteca(
    q: str = Query(..., min_length=3, max_length=300),
    max: int = Query(default=12, ge=1, le=30),
    fontes: list[str] = Query(default=["pubmed", "semantic", "openalex", "crossref"]),
    user: Usuario = Depends(get_verified_user),
):
    """Busca literatura em múltiplas bases e retorna resultados deduplicados."""
    try:
        return await buscar_literatura(q, max_results=max, fontes=fontes)
    except ValueError:
        raise HTTPException(status_code=400, detail="Selecione pelo menos uma base de busca.")


@router.patch("/{sessao_id}/secao")
async def editar_secao(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Salva edição manual de uma seção do artigo."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if not sessao.resultado:
        raise HTTPException(status_code=400, detail="Artigo não gerado ainda.")

    secao = body.get("secao")
    conteudo = body.get("conteudo")
    SECOES_VALIDAS = {"introducao", "caso_clinico", "discussao", "conclusao", "palavras_chave", "resumo", "referencias"}
    if secao not in SECOES_VALIDAS:
        raise HTTPException(status_code=400, detail=f"Seção inválida.")

    # Type validation per section
    if secao in {"introducao", "caso_clinico", "discussao", "conclusao"}:
        if not isinstance(conteudo, list):
            raise HTTPException(status_code=400, detail="conteudo deve ser uma lista de parágrafos.")
    elif secao == "palavras_chave":
        if not isinstance(conteudo, list):
            raise HTTPException(status_code=400, detail="conteudo deve ser uma lista de palavras.")
    elif secao == "resumo":
        if not isinstance(conteudo, dict):
            raise HTTPException(status_code=400, detail="conteudo deve ser um dicionário {introducao, caso, discussao, conclusao}.")
    elif secao == "referencias":
        if not isinstance(conteudo, list):
            raise HTTPException(status_code=400, detail="conteudo deve ser uma lista de referências.")

    from sqlalchemy.orm.attributes import flag_modified

    resultado = dict(sessao.resultado)
    resultado[secao] = conteudo
    resultado["versao_edicao"] = resultado.get("versao_edicao", 1) + 1
    sessao.resultado = resultado
    sessao.docx_path = None
    flag_modified(sessao, "resultado")

    # ── Gera DOCX editado e armazena no banco ─────────────────────────────────
    docx_bytes_editado: Optional[bytes] = None
    try:
        docx_bytes_editado = await _gerar_bytes_do_resultado(sessao)
        sessao.docx_editado_bytes = docx_bytes_editado
        _log.info("DOCX editado salvo no banco: sessao=%s bytes=%d", sessao_id, len(docx_bytes_editado))
    except Exception as e:
        _log.warning("Falha ao gerar DOCX editado para sessão %s: %s — download usará fallback", sessao_id, e)

    await db.commit()
    return {
        "ok": True,
        "versao_edicao": resultado["versao_edicao"],
        "docx_editado_gerado": docx_bytes_editado is not None,
    }


@router.post("/{sessao_id}/melhorar-secao")
async def melhorar_secao(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Reescreve uma seção usando IA (consome tokens)."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if not sessao.resultado:
        raise HTTPException(status_code=400, detail="Artigo não gerado ainda.")

    secao = body.get("secao")
    conteudo_atual = body.get("conteudo_atual", "")
    instrucao = body.get("instrucao", "")
    SECOES_VALIDAS = {"introducao", "caso_clinico", "discussao", "conclusao"}
    if secao not in SECOES_VALIDAS:
        raise HTTPException(status_code=400, detail="Seção inválida.")

    from app.agents.editor import melhorar_secao_llm
    paragrafos = await melhorar_secao_llm(
        secao=secao,
        conteudo_atual=conteudo_atual,
        instrucao=instrucao,
        cko=sessao.cko or {},
    )

    resultado = dict(sessao.resultado)
    resultado[secao] = paragrafos
    resultado["versao_edicao"] = resultado.get("versao_edicao", 1) + 1
    sessao.resultado = resultado
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(sessao, "resultado")
    await db.commit()

    return {"ok": True, "conteudo": paragrafos, "versao_edicao": resultado["versao_edicao"]}


@router.post("/biblioteca/salvar")
async def salvar_pesquisa(
    body: SalvarPesquisaRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Salva uma busca ou artigo selecionado na biblioteca do usuário."""
    item = PesquisaSalva(
        user_id=user.id,
        tipo=body.tipo,
        query=body.query,
        operador=body.operador,
        fontes=body.fontes,
        artigos=body.artigos[:50],
        observacao=body.observacao,
    )
    db.add(item)
    await db.commit()
    return {"salvo": True, "id": item.id, "tipo": item.tipo}


@router.get("/biblioteca/artigos")
async def listar_artigos_salvos(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Lista artigos salvos pelo usuário (tipo artigo ou caso_clinico)."""
    result = await db.execute(
        select(PesquisaSalva)
        .where(PesquisaSalva.user_id == user.id)
        .where(PesquisaSalva.tipo.in_(["artigo", "caso_clinico"]))
        .order_by(desc(PesquisaSalva.created_at))
        .limit(100)
    )
    itens = result.scalars().all()
    artigos = []
    for item in itens:
        for art in (item.artigos or []):
            entry = dict(art)
            entry["tipo_salvo"] = item.tipo
            entry["observacao"] = item.observacao
            entry["salvo_em"] = item.created_at.isoformat() if item.created_at else None
            entry["pesquisa_id"] = item.id
            artigos.append(entry)
    return {"total": len(artigos), "artigos": artigos}


@router.post("/{sessao_id}/adicionar-referencia")
async def adicionar_referencia(
    sessao_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Adiciona uma referência bibliográfica ao resultado de um relato."""
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user.id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if not sessao.resultado:
        raise HTTPException(status_code=400, detail="Artigo não gerado ainda.")

    art = body.get("artigo", {})
    resultado = dict(sessao.resultado)
    refs = list(resultado.get("referencias", []))

    # Next reference number
    numeros = [r.get("numero", 0) if isinstance(r, dict) else 0 for r in refs]
    proximo = (max(numeros) + 1) if numeros else 1

    # Build Vancouver-formatted citation
    autores = art.get("autores") or ""
    titulo = art.get("titulo") or ""
    periodico = art.get("periodico_abrev") or art.get("periodico") or ""
    ano = art.get("ano") or ""
    volume = art.get("volume") or ""
    numero_ed = art.get("numero") or ""
    paginas = art.get("paginas") or ""
    doi = art.get("doi") or ""

    formatada = f"{autores}. {titulo.rstrip('.')}. {periodico}. {ano}"
    if volume: formatada += f";{volume}"
    if numero_ed: formatada += f"({numero_ed})"
    if paginas: formatada += f":{paginas}"
    formatada += "."
    if doi: formatada += f" doi: {doi}"

    nova_ref = {
        "numero": proximo,
        "autores": autores,
        "titulo": titulo,
        "periodico": periodico,
        "ano": ano,
        "volume": volume or None,
        "paginas": paginas or None,
        "doi": doi or None,
        "pmid": art.get("pmid") or None,
        "formatada": art.get("formatada") or formatada,
    }

    refs.append(nova_ref)
    resultado["referencias"] = refs
    resultado["versao_edicao"] = resultado.get("versao_edicao", 1) + 1
    sessao.resultado = resultado
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(sessao, "resultado")
    await db.commit()

    return {"ok": True, "numero": proximo, "ref": nova_ref, "versao_edicao": resultado["versao_edicao"]}
