"""
Router de autenticação.
Endpoints: register, verify-email, login, refresh, logout,
           forgot-password, reset-password, me, Google OAuth,
           checkout (Stripe), planos.
"""
import asyncio
import secrets
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.database import get_db, Usuario, PLANO_PRO, PLANO_INSTITUCIONAL
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
    enviar_verificacao_email, enviar_reset_senha, enviar_boas_vindas_pro,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO   = "https://www.googleapis.com/oauth2/v3/userinfo"

_REFRESH_COOKIE    = "rccs_refresh"
_COOKIE_OPTS: dict = dict(httponly=True, secure=False, samesite="lax", max_age=30 * 86400)


def _set_refresh_cookie(response: Response, token: str):
    opts = {**_COOKIE_OPTS, "secure": settings.environment == "production"}
    response.set_cookie(_REFRESH_COOKIE, token, **opts)


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
        is_verified=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_verification_token(user.email)
    asyncio.create_task(enviar_verificacao_email(user.email, user.nome, token))

    return {"mensagem": "Conta criada. Verifique seu e-mail para ativar o acesso."}


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
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Usuario).where(Usuario.email == body.email.lower()))
    user   = result.scalar_one_or_none()

    if not user or not user.hashed_pw or not verify_password(body.senha, user.hashed_pw):
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Conta desativada. Entre em contato com o suporte.")

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

@router.post("/forgot-password", status_code=200)
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Usuario).where(Usuario.email == body.email.lower()))
    user   = result.scalar_one_or_none()
    # Resposta genérica — não revelar se e-mail existe (segurança)
    if user and user.hashed_pw:
        token = create_reset_token(user.email)
        asyncio.create_task(enviar_reset_senha(user.email, user.nome, token))
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
    response.set_cookie("oauth_state", state, httponly=True, max_age=300,
                        secure=settings.environment == "production")
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

    access  = create_access_token(user.id, user.email, user.plano)
    refresh = create_refresh_token(user.id)

    redirect = RedirectResponse(
        f"{settings.frontend_url}/index.html?token={access}"
    )
    _set_refresh_cookie(redirect, refresh)
    redirect.delete_cookie("oauth_state")
    return redirect


# ── Planos ────────────────────────────────────────────────────────────────────

@router.get("/planos")
async def listar_planos():
    return {
        "planos": [
            {"id": "free",          "nome": "Gratuito",       "artigos_mes": 1,    "preco_brl": 0},
            {"id": "pro",           "nome": "Pro",            "artigos_mes": 30,   "preco_brl": 9700},
            {"id": "institucional", "nome": "Institucional",  "artigos_mes": None, "preco_brl": 49700},
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

    price_map = {
        PLANO_PRO:           settings.stripe_pro_price_id,
        PLANO_INSTITUCIONAL: settings.stripe_inst_price_id,
    }
    price_id = price_map.get(body.plano)
    if not price_id:
        raise HTTPException(status_code=400, detail="Plano inválido.")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=user.id,
        customer_email=user.email,
        success_url=f"{settings.frontend_url}/index.html?upgrade=ok",
        cancel_url=f"{settings.frontend_url}/login.html?upgrade=cancel",
    )
    return {"url": session.url}
