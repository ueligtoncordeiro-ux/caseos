"""
Painel administrativo — rotas exclusivas para is_admin=True.

Segurança:
  • Todas as rotas dependem de get_admin_user (verifica is_admin no BD, não no JWT).
  • is_admin é verificado a cada request — revogação imediata sem reissue de token.
  • Endpoints destrutivos (DELETE) exigem confirmação explícita via query param.
  • Logs de auditoria em cada ação sensível.
"""
import asyncio
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


# ── Stripe ────────────────────────────────────────────────────────────────────

# Preços mensais BRL por plano (sincronizados com Stripe)
_PRECO_MENSAL = {"starter": 49.0, "pro": 99.0, "institucional": 349.0}


def _stripe_client():
    """Retorna o módulo stripe configurado. Levanta 400 se chave ausente."""
    from app.config import settings as _s
    if not _s.stripe_secret_key:
        raise HTTPException(400, "Stripe não configurado neste ambiente.")
    import stripe as _stripe
    _stripe.api_key = _s.stripe_secret_key
    return _stripe


@router.get("/stripe/overview")
async def stripe_overview(
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """
    MRR estimado via banco (por plano × preço) +
    dados em tempo real da Stripe API (past_due, novos assinantes, ARR).
    """
    # ── 1. MRR via banco (sempre rápido) ──────────────────────────────────────
    mrr = 0.0
    breakdown: dict = {}
    for plano, preco in _PRECO_MENSAL.items():
        cnt = (await db.execute(
            select(func.count(Usuario.id)).where(
                Usuario.plano == plano,
                Usuario.is_active == True,         # noqa: E712
            )
        )).scalar() or 0
        breakdown[plano] = {"count": cnt, "revenue": round(cnt * preco, 2)}
        mrr += cnt * preco

    # ── 2. Stripe API (opcional — falha graciosamente) ────────────────────────
    stripe_live: dict = {"ok": False, "error": None,
                         "past_due": 0, "new_this_month": 0,
                         "canceled_this_month": 0, "arr": 0.0}
    try:
        stripe = _stripe_client()

        def _fetch_stripe() -> dict:
            now = datetime.now(timezone.utc)
            month_start_ts = int(
                now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
            )

            # Past due
            pd_page = stripe.Subscription.list(status="past_due", limit=100)
            past_due = len(pd_page.data)

            # Novos assinantes (ativos criados neste mês)
            new_page = stripe.Subscription.list(
                created={"gte": month_start_ts}, status="active", limit=100
            )
            new_this_month = len(new_page.data)

            # Cancelados neste mês
            canc_page = stripe.Subscription.list(
                status="canceled", limit=100,
                # canceled_at não aceita filtro direto na list; usamos created como proxy
            )
            canceled_this_month = sum(
                1 for s in canc_page.data
                if (s.canceled_at or 0) >= month_start_ts
            )

            # ARR: MRR × 12 (usando os dados do banco já calculados)
            return {
                "ok": True,
                "past_due": past_due,
                "new_this_month": new_this_month,
                "canceled_this_month": canceled_this_month,
            }

        stripe_live.update(await asyncio.to_thread(_fetch_stripe))

    except HTTPException as e:
        stripe_live["error"] = e.detail
    except Exception as e:
        stripe_live["error"] = str(e)[:150]

    mrr_rounded = round(mrr, 2)
    return {
        "mrr": mrr_rounded,
        "arr": round(mrr_rounded * 12, 2),
        "breakdown": breakdown,
        "stripe": stripe_live,
    }


@router.get("/stripe/user/{user_id}")
async def stripe_user_detail(
    user_id: str,
    admin: Usuario = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Detalhes Stripe de um usuário: assinatura ativa, faturas recentes,
    método de pagamento e link direto para o dashboard Stripe.
    """
    user = await _get_usuario_ou_404(user_id, db)

    if not user.stripe_customer_id:
        return {"has_stripe": False, "message": "Usuário sem conta Stripe."}

    stripe = _stripe_client()

    def _fetch_user() -> dict:
        # Assinatura mais recente (qualquer status)
        subs = stripe.Subscription.list(
            customer=user.stripe_customer_id,
            limit=1,
            status="all",
            expand=["data.default_payment_method"],
        )
        sub = subs.data[0] if subs.data else None
        sub_data = None
        pm_data = None

        if sub:
            sub_data = {
                "id": sub.id,
                "status": sub.status,
                "current_period_start": sub.current_period_start,
                "current_period_end": sub.current_period_end,
                "cancel_at_period_end": sub.cancel_at_period_end,
                "canceled_at": sub.canceled_at,
            }
            # Cartão da assinatura
            pm = sub.get("default_payment_method")
            if pm and hasattr(pm, "card"):
                pm_data = {
                    "brand": pm.card.brand,
                    "last4": pm.card.last4,
                    "exp_month": pm.card.exp_month,
                    "exp_year": pm.card.exp_year,
                }

        # Faturas recentes (até 5)
        invs = stripe.Invoice.list(customer=user.stripe_customer_id, limit=5)
        inv_data = [
            {
                "id": inv.id,
                "number": getattr(inv, "number", None),
                "amount_paid": (inv.amount_paid or 0) / 100,
                "amount_due": (inv.amount_due or 0) / 100,
                "status": inv.status,
                "created": inv.created,
                "hosted_invoice_url": getattr(inv, "hosted_invoice_url", None),
            }
            for inv in invs.data
        ]

        # Cartão via PaymentMethod se não veio da assinatura
        if not pm_data:
            try:
                pms = stripe.PaymentMethod.list(
                    customer=user.stripe_customer_id, type="card"
                )
                if pms.data:
                    pm = pms.data[0]
                    pm_data = {
                        "brand": pm.card.brand,
                        "last4": pm.card.last4,
                        "exp_month": pm.card.exp_month,
                        "exp_year": pm.card.exp_year,
                    }
            except Exception:
                pass

        return {
            "has_stripe": True,
            "customer_id": user.stripe_customer_id,
            "stripe_url": f"https://dashboard.stripe.com/customers/{user.stripe_customer_id}",
            "subscription": sub_data,
            "invoices": inv_data,
            "payment_method": pm_data,
        }

    return await asyncio.to_thread(_fetch_user)
