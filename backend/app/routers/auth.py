"""
Router de autenticação.
Endpoints: register, verify-email, login, refresh, logout,
           forgot-password, reset-password, me, Google OAuth,
           checkout (Stripe), planos.
"""
import asyncio
import secrets
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from pydantic import BaseModel, Field
from app.config import settings
from app.models.database import get_db, Usuario, PLANO_STARTER, PLANO_PRO, PLANO_INSTITUCIONAL
from app.models.schemas import (
    RegisterRequest, LoginRequest, ForgotPasswordRequest,
    ResetPasswordRequest, UpdateProfileRequest,
    TokenResponse, UsuarioPublico, CheckoutRequest,
)
from app.services.auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    create_verification_token, create_reset_token,
    decode_token, get_current_user, get_verified_user,
    get_user_from_refresh,
)
from app.services.email import (
    enviar_verificacao_email, enviar_reset_senha,
    enviar_boas_vindas, enviar_boas_vindas_pro, enviar_aviso_login_google,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# ── Rate limiting para /auth/login (brute-force guard) ────────────────────────
# Instância única do Render: in-memory é suficiente. Em multi-instância usar Redis.
_LOGIN_MAX        = 10   # tentativas por IP na janela
_LOGIN_JANELA_SEG = 900  # 15 minutos

_login_hits: dict[str, list[float]] = defaultdict(list)


def _check_login_rate_limit(request: Request) -> None:
    """
    Bloqueia IPs que excedem _LOGIN_MAX tentativas de login em _LOGIN_JANELA_SEG segundos.
    Conta todas as tentativas (válidas e inválidas) — um usuário legítimo raramente faz
    mais de 2-3 logins por sessão, então 10/15 min é seguro sem falsos positivos.
    Adiciona header Retry-After para conformidade com RFC 6585.
    """
    ip = request.client.host if request.client else "unknown"
    agora = time.time()
    janela_inicio = agora - _LOGIN_JANELA_SEG

    # Remove timestamps fora da janela
    _login_hits[ip] = [t for t in _login_hits[ip] if t > janela_inicio]

    if len(_login_hits[ip]) >= _LOGIN_MAX:
        retry_after = int(_login_hits[ip][0] + _LOGIN_JANELA_SEG - agora) + 1
        raise HTTPException(
            status_code=429,
            detail=(
                f"Muitas tentativas de login neste IP. "
                f"Aguarde {max(1, retry_after // 60)} min e tente novamente."
            ),
            headers={"Retry-After": str(retry_after)},
        )
    _login_hits[ip].append(agora)


# ── Price IDs hardcoded como fallback — evita NameError caso env vars não estejam configuradas.
# Os valores de settings.stripe_*_price_id têm precedência quando definidos.
def _get_price_ids() -> dict:
    return {
        "starter":       settings.stripe_starter_price_id or "price_1TZxQv3oJrMmxd1m332GRzl2",
        "pro":           settings.stripe_pro_price_id      or "price_1TZxR43oJrMmxd1m0BsTg21X",
        "institucional": settings.stripe_inst_price_id     or "price_1TZxR73oJrMmxd1m1ZVKBsDS",
    }


class TrocarSenhaRequest(BaseModel):
    senha_atual: str
    senha_nova: str = Field(..., min_length=8, description="Mínimo 8 caracteres")

_GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO   = "https://www.googleapis.com/oauth2/v3/userinfo"

_REFRESH_COOKIE    = "rccs_refresh"

def _set_refresh_cookie(response: Response, token: str):
    is_prod = settings.environment == "production"
    # Em produção: SameSite=None + Secure=True para funcionar cross-domain
    # (Cloudflare Pages → Render). Em dev: Lax sem Secure.
    response.set_cookie(
        _REFRESH_COOKIE,
        token,
        httponly=True,
        secure=is_prod,
        samesite="none" if is_prod else "lax",
        max_age=30 * 86400,
    )


def _clear_refresh_cookie(response: Response):
    response.delete_cookie(_REFRESH_COOKIE)


def _token_response(response: Response, user: Usuario) -> TokenResponse:
    access  = create_access_token(user.id, user.email, user.plano)
    refresh = create_refresh_token(user.id)
    _set_refresh_cookie(response, refresh)
    return TokenResponse(access_token=access, usuario=UsuarioPublico.model_validate(user))


# ── Cadastro ──────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(Usuario).where(Usuario.email == body.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="E-mail já cadastrado.")

    user = Usuario(
        email=body.email.lower().strip(),
        nome=body.nome.strip(),
        hashed_pw=hash_password(body.senha),
        crm_cro=body.crm_cro,
        lgpd_aceito_em=datetime.utcnow(),
        is_verified=False,  # aguarda confirmação por e-mail
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Envia e-mail de verificação (fire-and-forget)
    token_verificacao = create_verification_token(user.email)
    asyncio.create_task(enviar_verificacao_email(user.email, user.nome, token_verificacao))

    return {"mensagem": "Conta criada! Verifique seu e-mail para ativar o acesso."}


# ── Verificação de e-mail ─────────────────────────────────────────────────────

@router.get("/verify")
async def verify_email(token: str, db: AsyncSession = Depends(get_db)):
    payload = decode_token(token, "verify")
    email   = payload.get("sub", "").lower()

    result = await db.execute(select(Usuario).where(Usuario.email == email))
    user   = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    if user.is_verified:
        return RedirectResponse(f"{settings.frontend_url}/login.html?msg=already_verified")

    user.is_verified = True
    await db.commit()
    return RedirectResponse(f"{settings.frontend_url}/login.html?msg=verified")


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # Rate limit primeiro — antes de qualquer consulta ao banco
    _check_login_rate_limit(request)

    result = await db.execute(select(Usuario).where(Usuario.email == body.email.lower()))
    user   = result.scalar_one_or_none()

    if not user or not user.hashed_pw or not verify_password(body.senha, user.hashed_pw):
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Conta desativada. Entre em contato com o suporte.")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Confirme seu e-mail antes de entrar. Verifique sua caixa de entrada.")

    return _token_response(response, user)


# ── Refresh token ─────────────────────────────────────────────────────────────

@router.post("/refresh", response_model=TokenResponse)
async def refresh(response: Response, user: Usuario = Depends(get_user_from_refresh)):
    return _token_response(response, user)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/logout", status_code=204)
async def logout(response: Response):
    _clear_refresh_cookie(response)
    return Response(status_code=204)


# ── Recuperação de senha ──────────────────────────────────────────────────────

_forgot_hits: dict[str, list[float]] = defaultdict(list)
_FORGOT_MAX        = 5
_FORGOT_JANELA_SEG = 3600  # 1 hora


def _check_forgot_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    agora = time.time()
    _forgot_hits[ip] = [t for t in _forgot_hits[ip] if t > agora - _FORGOT_JANELA_SEG]
    if len(_forgot_hits[ip]) >= _FORGOT_MAX:
        raise HTTPException(
            status_code=429,
            detail="Muitas solicitações de recuperação de senha. Aguarde 1 hora.",
            headers={"Retry-After": "3600"},
        )
    _forgot_hits[ip].append(agora)


@router.post("/forgot-password", status_code=200)
async def forgot_password(request: Request, body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    _check_forgot_rate_limit(request)
    result = await db.execute(select(Usuario).where(Usuario.email == body.email.lower()))
    user   = result.scalar_one_or_none()
    # Resposta genérica — não revelar se e-mail existe (segurança)
    if user and user.is_active:
        if user.hashed_pw:
            # Conta com senha → envia link de redefinição
            token = create_reset_token(user.email)
            asyncio.create_task(enviar_reset_senha(user.email, user.nome, token))
        elif user.google_sub:
            # Conta Google-only → avisa que o login é via Google
            asyncio.create_task(enviar_aviso_login_google(user.email, user.nome))
    return {"mensagem": "Se o e-mail estiver cadastrado, você receberá um link em instantes."}


@router.post("/reset-password", status_code=200)
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    payload = decode_token(body.token, "reset")
    email   = payload.get("sub", "").lower()

    result = await db.execute(select(Usuario).where(Usuario.email == email))
    user   = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    user.hashed_pw = hash_password(body.nova_senha)
    await db.commit()
    return {"mensagem": "Senha redefinida com sucesso. Faça login."}


# ── Perfil ────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UsuarioPublico)
async def me(user: Usuario = Depends(get_current_user)):
    return UsuarioPublico.model_validate(user)


@router.get("/me/tokens")
async def me_tokens(user: Usuario = Depends(get_current_user)):
    """Retorna situação atual de tokens do usuário."""
    from app.models.database import TOKENS_LIMITE
    limite = TOKENS_LIMITE.get(user.plano, 50_000)
    usados = user.tokens_mes or 0
    restantes = max(0, limite - usados)
    artigos_equiv_restantes = restantes // 25_000
    pct = min(100, round(usados / limite * 100)) if limite > 0 else 0
    return {
        "plano": user.plano,
        "tokens_mes": usados,
        "tokens_limite": limite,
        "tokens_restantes": restantes,
        "pct_usado": pct,
        "artigos_equiv_restantes": artigos_equiv_restantes,
        "reset_em": _proximo_reset(),
    }


def _proximo_reset() -> str:
    """Retorna string 'DD/MM' da virada do próximo mês."""
    from datetime import date
    hoje = date.today()
    if hoje.month == 12:
        proximo = date(hoje.year + 1, 1, 1)
    else:
        proximo = date(hoje.year, hoje.month + 1, 1)
    return proximo.strftime("%d/%m")


@router.patch("/me", response_model=UsuarioPublico)
async def update_me(
    body: UpdateProfileRequest,
    user: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.nome:
        user.nome = body.nome.strip()
    if body.crm_cro is not None:
        user.crm_cro = body.crm_cro
    await db.commit()
    await db.refresh(user)
    return UsuarioPublico.model_validate(user)


@router.delete("/me", status_code=204)
async def delete_me(
    response: Response,
    user: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """LGPD: exclusão de conta e dados pessoais."""
    user.is_active  = False
    user.email      = f"deleted_{user.id}@rccs.deleted"
    user.nome       = "Conta excluída"
    user.hashed_pw  = None
    user.google_sub = None
    await db.commit()
    _clear_refresh_cookie(response)
    return Response(status_code=204)


# ── Troca de senha autenticada ────────────────────────────────────────────────

@router.post("/senha", status_code=200)
async def trocar_senha(
    body: TrocarSenhaRequest,
    user: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Troca senha com confirmação da senha atual. Exige estar autenticado."""
    if not user.hashed_pw:
        raise HTTPException(
            status_code=400,
            detail="Conta criada via Google não possui senha local. "
                   "Use 'Esqueci minha senha' para definir uma.",
        )
    if not verify_password(body.senha_atual, user.hashed_pw):
        raise HTTPException(status_code=400, detail="Senha atual incorreta.")

    user.hashed_pw = hash_password(body.senha_nova)
    await db.commit()
    return {"mensagem": "Senha alterada com sucesso."}


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.get("/google")
async def google_login(response: Response):
    if not settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google OAuth não configurado.")

    state = secrets.token_urlsafe(32)
    # Guarda state em cookie httpOnly para validação no callback (CSRF)
    response = RedirectResponse(
        url=(
            f"{_GOOGLE_AUTH_URL}?"
            f"client_id={settings.google_client_id}"
            f"&redirect_uri={settings.google_redirect_uri}"
            f"&response_type=code"
            f"&scope=openid+email+profile"
            f"&state={state}"
            f"&access_type=offline"
        )
    )
    # SameSite=None; Secure obrigatório para redirect chain cross-site
    # (iOS Safari / ITP bloqueia cookies com SameSite=Lax em fluxos OAuth)
    is_https = settings.environment == "production"
    response.set_cookie(
        "oauth_state", state,
        httponly=True,
        max_age=300,
        secure=is_https,
        samesite="none" if is_https else "lax",
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    response: Response,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    # Validar state (CSRF)
    cookie_state = request.cookies.get("oauth_state", "")
    if not secrets.compare_digest(state, cookie_state):
        raise HTTPException(status_code=400, detail="Estado OAuth inválido.")

    async with httpx.AsyncClient() as client:
        # Trocar code por tokens
        token_resp = await client.post(_GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri":  settings.google_redirect_uri,
            "grant_type":    "authorization_code",
        })
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Falha ao obter token do Google.")
        tokens = token_resp.json()

        # Obter dados do usuário
        user_resp = await client.get(
            _GOOGLE_USERINFO,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        guser = user_resp.json()

    email = guser.get("email", "").lower()
    sub   = guser.get("sub")
    nome  = guser.get("name", email.split("@")[0].capitalize())

    if not email or not sub:
        raise HTTPException(status_code=400, detail="Dados insuficientes do Google.")

    # Upsert usuário
    result = await db.execute(
        select(Usuario).where((Usuario.email == email) | (Usuario.google_sub == sub))
    )
    user = result.scalar_one_or_none()

    is_new_user = user is None

    if user:
        user.google_sub     = sub
        user.google_picture = guser.get("picture")
        user.is_verified    = True
    else:
        user = Usuario(
            email=email, nome=nome,
            google_sub=sub,
            google_picture=guser.get("picture"),
            is_verified=True,
            lgpd_aceito_em=datetime.utcnow(),
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)

    # Boas-vindas apenas para novos usuários Google
    if is_new_user:
        asyncio.create_task(enviar_boas_vindas(user.email, user.nome, via_google=True))

    access  = create_access_token(user.id, user.email, user.plano)
    refresh = create_refresh_token(user.id)

    redirect = RedirectResponse(
        f"{settings.frontend_url}/login.html?token={access}"
    )
    _set_refresh_cookie(redirect, refresh)
    redirect.delete_cookie("oauth_state")
    return redirect


# ── Planos ────────────────────────────────────────────────────────────────────

@router.get("/planos")
async def listar_planos():
    from app.models.database import TOKENS_LIMITE, PRECO_BRL, QUOTA_MENSAL
    return {
        "planos": [
            {
                "id":            "free",
                "nome":          "Gratuito",
                "artigos_mes":   QUOTA_MENSAL["free"],
                "tokens_limite": TOKENS_LIMITE["free"],
                "preco_brl":     PRECO_BRL["free"],
                "descricao":     "Prévias demo ilimitadas · 1 artigo/mês · 50k tokens",
            },
            {
                "id":            "starter",
                "nome":          "Starter",
                "artigos_mes":   QUOTA_MENSAL["starter"],
                "tokens_limite": TOKENS_LIMITE["starter"],
                "preco_brl":     PRECO_BRL["starter"],
                "descricao":     "~6 artigos completos/mês · 150k tokens · CARE completo",
            },
            {
                "id":            "pro",
                "nome":          "Pro",
                "artigos_mes":   QUOTA_MENSAL["pro"],
                "tokens_limite": TOKENS_LIMITE["pro"],
                "preco_brl":     PRECO_BRL["pro"],
                "descricao":     "~12 artigos/mês · 300k tokens · PubMed/OpenAlex/S2",
            },
            {
                "id":            "institucional",
                "nome":          "Institucional",
                "artigos_mes":   QUOTA_MENSAL["institucional"],
                "tokens_limite": TOKENS_LIMITE["institucional"],
                "preco_brl":     PRECO_BRL["institucional"],
                "descricao":     "~60 artigos/mês · 1,5M tokens · Multi-usuário",
            },
        ]
    }


# ── Stripe Checkout ───────────────────────────────────────────────────────────

@router.post("/checkout")
async def criar_checkout(
    body: CheckoutRequest,
    user: Usuario = Depends(get_verified_user),
):
    import stripe
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=501, detail="Pagamentos não configurados.")

    stripe.api_key = settings.stripe_secret_key

    # Bloqueia tentativa de comprar o plano que o usuário já possui
    if user.plano == body.plano:
        raise HTTPException(
            status_code=409,
            detail=f"Você já possui o plano '{body.plano}'. "
                   "Para gerenciar sua assinatura acesse o portal de faturamento.",
        )

    price_map = _get_price_ids()
    price_id = price_map.get(body.plano)
    if not price_id:
        raise HTTPException(status_code=400, detail="Plano inválido.")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=str(user.id),
            customer_email=user.email,
            # metadata.plano permite que o webhook identifique o plano correto
            metadata={"plano": body.plano, "user_id": str(user.id)},
            subscription_data={"metadata": {"plano": body.plano, "user_id": str(user.id)}},
            # Idioma e moeda BRL
            locale="pt-BR",
            success_url=f"{settings.frontend_url}/dashboard.html?upgrade=ok&plano={body.plano}",
            cancel_url=f"{settings.frontend_url}/dashboard.html?upgrade=cancel",
        )
        return {"url": session.url}
    except Exception as e:
        err = getattr(e, "user_message", None) or getattr(e, "error", {})
        detail = err.get("message", str(e)) if isinstance(err, dict) else str(e)
        raise HTTPException(status_code=402, detail=f"Stripe: {detail}")


# ── Stripe Customer Portal ────────────────────────────────────────────────────

@router.post("/billing-portal")
async def billing_portal(
    user: Usuario = Depends(get_verified_user),
):
    """
    Cria uma sessão no Stripe Customer Portal para o usuário gerenciar
    sua assinatura (trocar plano, cancelar, atualizar cartão etc.).
    Exige stripe_customer_id preenchido — usuários sem assinatura ativa
    recebem 400 orientando a fazer upgrade primeiro.
    """
    import stripe
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=501, detail="Pagamentos não configurados.")

    if not user.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="Nenhuma assinatura encontrada. Faça upgrade para um plano pago primeiro.",
        )

    stripe.api_key = settings.stripe_secret_key

    try:
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{settings.frontend_url}/dashboard.html",
        )
        return {"url": session.url}
    except Exception as e:
        err = getattr(e, "user_message", None) or getattr(e, "error", {})
        detail = err.get("message", str(e)) if isinstance(err, dict) else str(e)
        raise HTTPException(status_code=402, detail=f"Stripe: {detail}")
