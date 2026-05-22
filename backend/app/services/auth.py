"""
Serviço de autenticação.
JWT (access + refresh + verify + reset), bcrypt, dependências FastAPI.
"""
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Cookie, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.database import get_db, Usuario, QUOTA_MENSAL, TOKENS_LIMITE, PLANO_FREE

# ── Hashing ───────────────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Token creation ────────────────────────────────────────────────────────────

def _make_token(payload: dict, expires: timedelta) -> str:
    data = {**payload, "exp": datetime.utcnow() + expires}
    return jwt.encode(data, settings.secret_key, algorithm=settings.algorithm)

def create_access_token(user_id: str, email: str, plano: str) -> str:
    return _make_token(
        {"sub": user_id, "email": email, "plano": plano, "type": "access"},
        timedelta(minutes=settings.access_token_expire_minutes),
    )

def create_refresh_token(user_id: str) -> str:
    return _make_token(
        {"sub": user_id, "type": "refresh"},
        timedelta(days=settings.refresh_token_expire_days),
    )

def create_verification_token(email: str) -> str:
    return _make_token(
        {"sub": email, "type": "verify"},
        timedelta(hours=24),
    )

def create_reset_token(email: str) -> str:
    return _make_token(
        {"sub": email, "type": "reset"},
        timedelta(hours=1),
    )

def decode_token(token: str, expected_type: str) -> dict:
    """Decodifica e valida um JWT. Lança HTTPException 401 em falha."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        if payload.get("type") != expected_type:
            raise JWTError("wrong type")
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── FastAPI dependencies ──────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)

async def _get_user_by_id(user_id: str, db: AsyncSession) -> Optional[Usuario]:
    result = await db.execute(select(Usuario).where(Usuario.id == user_id))
    return result.scalar_one_or_none()


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    token_query: Optional[str] = None,   # para WebSocket via query param
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    raw = credentials.credentials if credentials else token_query
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticação necessária.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(raw, "access")
    user = await _get_user_by_id(payload["sub"], db)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Usuário não encontrado ou desativado.")
    return user


async def get_verified_user(user: Usuario = Depends(get_current_user)) -> Usuario:
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verifique seu e-mail antes de usar a plataforma.",
        )
    return user


async def check_quota(
    user: Usuario = Depends(get_verified_user),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    """Verifica quota mensal de artigos e tokens; reseta contadores no virada do mês."""
    mes_atual = datetime.utcnow().strftime("%Y-%m")

    # Reset mensal
    if user.mes_referencia != mes_atual:
        user.artigos_mes = 0
        user.tokens_mes  = 0
        user.mes_referencia = mes_atual

    # Verificar limite de artigos por plano
    limit_artigos = QUOTA_MENSAL.get(user.plano)
    if limit_artigos is not None and user.artigos_mes >= limit_artigos:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite do plano {user.plano.upper()} atingido "
                f"({limit_artigos} artigo{'s' if limit_artigos > 1 else ''}/mês). "
                "Acesse /precos para fazer upgrade."
            ),
        )

    # Verificar limite de tokens por plano
    limit_tokens = TOKENS_LIMITE.get(user.plano, 50_000)
    if user.tokens_mes >= limit_tokens:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de tokens do plano {user.plano.upper()} atingido "
                f"({limit_tokens:,} tokens/mês). "
                "Acesse /precos para fazer upgrade."
            ),
        )

    user.artigos_mes += 1
    await db.commit()
    return user


async def debitar_tokens(user_id: str, tokens: int) -> None:
    """Debita tokens do saldo mensal do usuário. Chamado ao final do pipeline."""
    if tokens <= 0:
        return
    from app.models.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Usuario).where(Usuario.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            mes_atual = datetime.utcnow().strftime("%Y-%m")
            if user.mes_referencia != mes_atual:
                user.tokens_mes = 0
                user.mes_referencia = mes_atual
            user.tokens_mes += tokens
            await db.commit()


async def get_user_from_refresh(
    rccs_refresh: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    if not rccs_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Refresh token ausente.")
    payload = decode_token(rccs_refresh, "refresh")
    user = await _get_user_by_id(payload["sub"], db)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Sessão inválida. Faça login novamente.")
    return user
