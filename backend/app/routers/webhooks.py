"""
Webhooks externos.
Stripe: verifica assinatura HMAC, atualiza plano do usuário e envia e-mail de boas-vindas.
"""
import asyncio
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.database import get_db, Usuario, PLANO_FREE, PLANO_STARTER, PLANO_PRO, PLANO_INSTITUCIONAL, TOKENS_LIMITE
from app.services.email import enviar_boas_vindas_pro

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

STRIPE_PRICE_TO_PLAN = {
    "price_1TZxQv3oJrMmxd1m332GRzl2": PLANO_STARTER,       # Starter R$49
    "price_1TZxR43oJrMmxd1m0BsTg21X": PLANO_PRO,           # Pro R$99
    "price_1TZxR73oJrMmxd1m1ZVKBsDS": PLANO_INSTITUCIONAL, # Institucional R$349
    "price_1TYHOA3oJrMmxd1m5HBpOtJp": PLANO_PRO,           # legado R$97
    "price_1TYHON3oJrMmxd1mJuzYqTm4": PLANO_INSTITUCIONAL, # legado R$497
}

# Mapeamento Stripe price_id → plano interno
def _plano_from_price(price_id: str) -> str:
    if price_id in STRIPE_PRICE_TO_PLAN:
        return STRIPE_PRICE_TO_PLAN[price_id]
    if price_id == settings.stripe_starter_price_id:
        return PLANO_STARTER
    if price_id == settings.stripe_pro_price_id:
        return PLANO_PRO
    if price_id == settings.stripe_inst_price_id:
        return PLANO_INSTITUCIONAL
    return PLANO_FREE


def _aplicar_plano(user: Usuario, novo_plano: str) -> None:
    """Troca o plano e reseta contadores mensais. Chame antes de db.commit()."""
    from datetime import datetime
    mes_atual = datetime.utcnow().strftime("%Y-%m")
    plano_anterior = user.plano

    user.plano = novo_plano

    # Reset de contadores apenas quando o plano MUDA (upgrade/downgrade)
    if plano_anterior != novo_plano:
        user.artigos_mes    = 0
        user.tokens_mes     = 0
        user.mes_referencia = mes_atual


def _verificar_assinatura_stripe(payload: bytes, sig_header: str, secret: str):
    """
    Verifica a assinatura Stripe-Signature sem usar o SDK stripe.
    Implementação manual conforme docs.stripe.com/webhooks/signatures.
    """
    if not secret:
        return  # Em dev sem secret configurado, pula verificação

    try:
        parts = {k: v for part in sig_header.split(",") for k, v in [part.split("=", 1)]}
        timestamp = int(parts["t"])
        signature = parts["v1"]
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail="Stripe-Signature malformado.")

    # Tolerância de 5 minutos (proteção replay)
    if abs(time.time() - timestamp) > 300:
        raise HTTPException(status_code=400, detail="Webhook expirado.")

    signed_payload = f"{timestamp}.{payload.decode()}".encode()
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="Assinatura Stripe inválida.")


@router.post("/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    body       = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    _verificar_assinatura_stripe(body, sig_header, settings.stripe_webhook_secret)

    import json
    event = json.loads(body)
    etype = event.get("type", "")
    data  = event.get("data", {}).get("object", {})

    # ── checkout.session.completed → primeira assinatura ──────────────────────
    if etype == "checkout.session.completed":
        user_id    = data.get("client_reference_id")
        customer   = data.get("customer")
        sub_id     = data.get("subscription")

        if not user_id:
            return {"received": True}

        result = await db.execute(select(Usuario).where(Usuario.id == user_id))
        user   = result.scalar_one_or_none()
        if not user:
            return {"received": True}

        # Plano via metadata da session (definido no checkout) ou fallback pelo price_id
        plano = (
            data.get("metadata", {}).get("plano")
            or _plano_from_price(
                # tenta extrair price_id do subscription expandido (se disponível)
                data.get("subscription_data", {}).get("metadata", {}).get("plano", "")
            )
            or PLANO_PRO  # fallback seguro
        )

        user.stripe_customer_id         = customer
        user.stripe_subscription_id     = sub_id
        user.stripe_subscription_status = "active"
        _aplicar_plano(user, plano)   # troca plano + reseta contadores
        await db.commit()

        asyncio.create_task(enviar_boas_vindas_pro(user.email, user.nome))

    # ── customer.subscription.updated → mudança de plano ou renovação ─────────
    elif etype == "customer.subscription.updated":
        sub_status = data.get("status")
        customer   = data.get("customer")
        price_id   = (data.get("items", {})
                       .get("data", [{}])[0]
                       .get("price", {})
                       .get("id", ""))

        result = await db.execute(
            select(Usuario).where(Usuario.stripe_customer_id == customer)
        )
        user = result.scalar_one_or_none()
        if user:
            user.stripe_subscription_status = sub_status
            if sub_status == "active":
                _aplicar_plano(user, _plano_from_price(price_id))
            elif sub_status in ("past_due", "unpaid", "canceled", "paused"):
                _aplicar_plano(user, PLANO_FREE)
            await db.commit()

    # ── customer.subscription.deleted → cancelamento / inadimplência ──────────
    elif etype == "customer.subscription.deleted":
        customer = data.get("customer")
        result   = await db.execute(
            select(Usuario).where(Usuario.stripe_customer_id == customer)
        )
        user = result.scalar_one_or_none()
        if user:
            _aplicar_plano(user, PLANO_FREE)
            user.stripe_subscription_id     = None
            user.stripe_subscription_status = "canceled"
            await db.commit()

    # ── invoice.payment_failed → aviso de cobrança falhou ─────────────────────
    elif etype == "invoice.payment_failed":
        # Por ora apenas loga; pode enviar email de aviso ao usuário
        pass

    return {"received": True}
