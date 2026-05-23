"""
Painel administrativo — rotas exclusivas para is_admin=True.

Segurança:
  • Todas as rotas dependem de get_admin_user (verifica is_admin no BD, não no JWT).
  • is_admin é verificado a cada request — revogação imediata sem reissue de token.
  • Endpoints destrutivos (DELETE) exigem confirmação explícita via query param.
  • Logs de auditoria em cada ação sensível.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    get_db, Usuario, Sessao,
    QUOTA_MENSAL, TOKENS_LIMITE,
    PLANO_FREE, PLANO_STARTER, PLANO_PRO, PLANO_INSTITUCIONAL,
)
from app.services.auth import get_admin_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Schemas de request/response ───────────────────────────────────────────────

class UsuarioAdminView(BaseModel):
    id: str
    email: str
    nome: str
    plano: str
    is_active: bool
    is_verified: bool
    is_admin: bool
    artigos_mes: int
    tokens_mes: int
    mes_referencia: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_subscription_status: Optional[str] = None
    crm_cro: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PatchUsuarioRequest(BaseModel):
    nome: Optional[str] = None
    plano: Optional[str] = None
    is_active: Optional[bool] = None
    is_verified: Optional[bool] = None
    is_admin: Optional[bool] = None
    artigos_mes: Optional[int] = None
    tokens_mes: Optional[int] = None


class SessaoAdminView(BaseModel):
    id: str
    external_id: str
    user_id: Optional[str] = None
    status: str
    titulo: Optional[str] = None
    care_score: Optional[int] = None
    tokens_usados: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Helpers ───────────────────────────────────────────────────────────────────

_PLANOS_VALIDOS = {PLANO_FREE, PLANO_STARTER, PLANO_PRO, PLANO_INSTITUCIONAL}


async def _get_usuario_ou_404(user_id: str, db: AsyncSession) -> Usuario:
    result = await db.execute(select(Usuario).where(Usuario.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    return user


# ── Visão geral ───────────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Estatísticas gerais da plataforma."""
    # Contagem de usuários por plano
    total = (await db.execute(func.count(Usuario.id))).scalar() or 0

    plano_counts: dict = {}
    for plano in _PLANOS_VALIDOS:
        cnt = (await db.execute(
            select(func.count(Usuario.id)).where(Usuario.plano == plano)
        )).scalar() or 0
        plano_counts[plano] = cnt

    pagantes = (await db.execute(
        select(func.count(Usuario.id)).where(Usuario.plano != PLANO_FREE)
    )).scalar() or 0

    verificados = (await db.execute(
        select(func.count(Usuario.id)).where(Usuario.is_verified == True)  # noqa: E712
    )).scalar() or 0

    ativos = (await db.execute(
        select(func.count(Usuario.id)).where(Usuario.is_active == True)  # noqa: E712
    )).scalar() or 0

    # Sessões
    total_sessoes = (await db.execute(func.count(Sessao.id))).scalar() or 0

    status_counts: dict = {}
    for st in ("concluido", "processando", "rascunho", "erro"):
        cnt = (await db.execute(
            select(func.count(Sessao.id)).where(Sessao.status == st)
        )).scalar() or 0
        status_counts[st] = cnt

    # Tokens totais consumidos (soma global)
    tokens_total = (await db.execute(
        select(func.sum(Usuario.tokens_mes))
    )).scalar() or 0

    artigos_total = (await db.execute(
        select(func.sum(Usuario.artigos_mes))
    )).scalar() or 0

    return {
        "usuarios": {
            "total": total,
            "ativos": ativos,
            "verificados": verificados,
            "pagantes": pagantes,
            "por_plano": plano_counts,
        },
        "sessoes": {
            "total": total_sessoes,
            "por_status": status_counts,
        },
        "consumo_mes": {
            "artigos": artigos_total,
            "tokens": tokens_total,
        },
    }


# ── Usuários ──────────────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UsuarioAdminView])
async def listar_usuarios(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None, description="Filtro por email ou nome (case-insensitive)"),
    plano: Optional[str] = Query(None),
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Lista todos os usuários com paginação e filtro opcional."""
    stmt = select(Usuario).order_by(Usuario.created_at.desc())

    if q:
        like = f"%{q.lower()}%"
        from sqlalchemy import or_
        stmt = stmt.where(
            or_(
                func.lower(Usuario.email).like(like),
                func.lower(Usuario.nome).like(like),
            )
        )

    if plano and plano in _PLANOS_VALIDOS:
        stmt = stmt.where(Usuario.plano == plano)

    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/users/{user_id}", response_model=UsuarioAdminView)
async def get_usuario(
    user_id: str,
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_usuario_ou_404(user_id, db)


@router.patch("/users/{user_id}", response_model=UsuarioAdminView)
async def atualizar_usuario(
    user_id: str,
    body: PatchUsuarioRequest,
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Atualiza campos de um usuário. Somente campos enviados são alterados."""
    user = await _get_usuario_ou_404(user_id, db)

    if body.plano is not None:
        if body.plano not in _PLANOS_VALIDOS:
            raise HTTPException(400, f"Plano inválido. Válidos: {_PLANOS_VALIDOS}")
        user.plano = body.plano

    if body.nome is not None:
        user.nome = body.nome.strip()

    if body.is_active is not None:
        user.is_active = body.is_active

    if body.is_verified is not None:
        user.is_verified = body.is_verified

    if body.is_admin is not None:
        # Impede que o admin remova seus próprios privilégios acidentalmente
        if user.id == admin.id and not body.is_admin:
            raise HTTPException(400, "Você não pode remover seus próprios privilégios de admin.")
        user.is_admin = body.is_admin

    if body.artigos_mes is not None:
        user.artigos_mes = max(0, body.artigos_mes)

    if body.tokens_mes is not None:
        user.tokens_mes = max(0, body.tokens_mes)

    await db.commit()
    await db.refresh(user)
    logger.warning(
        "ADMIN PATCH user=%s by admin=%s | changes=%s",
        user_id, admin.id, body.model_dump(exclude_none=True),
    )
    return user


@router.post("/users/{user_id}/reset-quota")
async def resetar_quota(
    user_id: str,
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Zera os contadores de artigos e tokens do mês do usuário."""
    user = await _get_usuario_ou_404(user_id, db)
    user.artigos_mes = 0
    user.tokens_mes = 0
    user.mes_referencia = datetime.now(timezone.utc).strftime("%Y-%m")
    await db.commit()
    logger.warning("ADMIN RESET QUOTA user=%s by admin=%s", user_id, admin.id)
    return {"ok": True, "artigos_mes": 0, "tokens_mes": 0}


@router.delete("/users/{user_id}")
async def deletar_usuario(
    user_id: str,
    confirmar: bool = Query(False, description="Passar ?confirmar=true para confirmar a exclusão"),
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Exclui um usuário e todas as suas sessões.
    Requer ?confirmar=true para evitar exclusões acidentais.
    Não é possível excluir o próprio admin logado.
    """
    if not confirmar:
        raise HTTPException(
            400,
            "Adicione ?confirmar=true para confirmar a exclusão. Esta ação é irreversível.",
        )

    if user_id == admin.id:
        raise HTTPException(400, "Você não pode excluir sua própria conta pelo painel admin.")

    user = await _get_usuario_ou_404(user_id, db)

    # Exclui sessões primeiro (FK)
    await db.execute(delete(Sessao).where(Sessao.user_id == user_id))
    await db.delete(user)
    await db.commit()

    logger.warning("ADMIN DELETE user=%s (%s) by admin=%s", user_id, user.email, admin.id)
    return {"ok": True, "excluido": user_id}


# ── Sessões ───────────────────────────────────────────────────────────────────

@router.get("/sessions", response_model=list[SessaoAdminView])
async def listar_sessoes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user_id: Optional[str] = Query(None, description="Filtrar por usuário"),
    st: Optional[str] = Query(None, alias="status", description="Filtrar por status"),
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Lista sessões (relatos clínicos) com paginação e filtro opcional."""
    stmt = select(Sessao).order_by(Sessao.created_at.desc())

    if user_id:
        stmt = stmt.where(Sessao.user_id == user_id)

    if st:
        stmt = stmt.where(Sessao.status == st)

    stmt = stmt.offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.delete("/sessions/{sessao_id}")
async def deletar_sessao(
    sessao_id: str,
    confirmar: bool = Query(False),
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Exclui uma sessão pelo ID interno."""
    if not confirmar:
        raise HTTPException(400, "Adicione ?confirmar=true para confirmar.")

    result = await db.execute(select(Sessao).where(Sessao.id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(404, "Sessão não encontrada.")

    await db.delete(sessao)
    await db.commit()
    logger.warning("ADMIN DELETE session=%s by admin=%s", sessao_id, admin.id)
    return {"ok": True, "excluido": sessao_id}
