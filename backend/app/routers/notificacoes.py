"""
Notificações in-app — CRUD básico.

Endpoints:
  GET  /notificacoes            → últimas 40 notificações do usuário
  GET  /notificacoes/nao-lidas  → contagem de não-lidas (para polling rápido)
  POST /notificacoes/marcar-todas-lidas
  PATCH /notificacoes/{id}/lida
  DELETE /notificacoes          → apaga todas as notificações lidas (limpeza)

Criação: feita internamente pelo orquestrador e webhooks via `criar_notificacao()`.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Notificacao, get_db, Usuario, AsyncSessionLocal
from app.services.auth import get_verified_user

router = APIRouter(prefix="/notificacoes", tags=["notificacoes"])
_log = logging.getLogger(__name__)


# ── Helper público — chamado pelo orquestrador e webhooks ─────────────────────

async def criar_notificacao(
    user_id: str,
    tipo: str,
    titulo: str,
    texto: str,
    link: str = "",
) -> None:
    """
    Cria uma notificação para o usuário. Falha silenciosamente para não
    bloquear o pipeline em caso de erro de banco.
    Mantém no máximo 100 notificações por usuário (apaga as mais antigas).
    """
    try:
        async with AsyncSessionLocal() as db:
            notif = Notificacao(
                user_id=user_id,
                tipo=tipo,
                titulo=titulo,
                texto=texto,
                link=link or None,
            )
            db.add(notif)
            await db.flush()  # obtém o id antes de commit

            # Limita a 100 notificações por usuário — apaga excedente mais antigo
            subq = (
                select(Notificacao.id)
                .where(Notificacao.user_id == user_id)
                .order_by(Notificacao.created_at.desc())
                .offset(100)
            )
            await db.execute(delete(Notificacao).where(Notificacao.id.in_(subq)))
            await db.commit()
    except Exception as exc:
        _log.warning("Falha ao criar notificação para user=%s: %s", user_id, exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _fmt(n: Notificacao) -> dict:
    from app.models.database import NOTIF_TIPOS
    emoji = NOTIF_TIPOS.get(n.tipo, "🔔")
    return {
        "id":         n.id,
        "tipo":       n.tipo,
        "emoji":      emoji,
        "titulo":     n.titulo,
        "texto":      n.texto,
        "lida":       n.lida,
        "link":       n.link,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("")
async def listar_notificacoes(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Retorna as últimas 40 notificações (lidas e não-lidas)."""
    result = await db.execute(
        select(Notificacao)
        .where(Notificacao.user_id == user.id)
        .order_by(Notificacao.created_at.desc())
        .limit(40)
    )
    notifs = result.scalars().all()
    nao_lidas = sum(1 for n in notifs if not n.lida)
    return {
        "total":      len(notifs),
        "nao_lidas":  nao_lidas,
        "itens":      [_fmt(n) for n in notifs],
    }


@router.get("/nao-lidas")
async def contar_nao_lidas(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Contagem rápida de não-lidas — endpoint leve para polling."""
    result = await db.execute(
        select(func.count())
        .select_from(Notificacao)
        .where(Notificacao.user_id == user.id, Notificacao.lida == False)  # noqa: E712
    )
    count = result.scalar() or 0
    return {"nao_lidas": count}


@router.post("/marcar-todas-lidas")
async def marcar_todas_lidas(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await db.execute(
        update(Notificacao)
        .where(Notificacao.user_id == user.id, Notificacao.lida == False)  # noqa: E712
        .values(lida=True)
    )
    await db.commit()
    return {"ok": True}


@router.patch("/{notif_id}/lida")
async def marcar_lida(
    notif_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    result = await db.execute(
        select(Notificacao).where(
            Notificacao.id == notif_id,
            Notificacao.user_id == user.id,
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="Notificação não encontrada.")
    notif.lida = True
    await db.commit()
    return {"ok": True, "id": notif_id}


@router.delete("")
async def limpar_notificacoes_lidas(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    """Remove todas as notificações já lidas do usuário."""
    await db.execute(
        delete(Notificacao).where(
            Notificacao.user_id == user.id,
            Notificacao.lida == True,  # noqa: E712
        )
    )
    await db.commit()
    return {"ok": True}
