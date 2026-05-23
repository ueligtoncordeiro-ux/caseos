from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional
import logging

from app.config import settings
from app.models.database import init_db
from app.services.websocket_manager import manager
from app.services.auth import decode_token
from app.routers import artigo, auth, webhooks, chat, imagens

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Validações de startup (produção) ─────────────────────────────────────
    if settings.environment == "production":
        _DEFAULT_KEY = "dev-secret-key-troque-em-producao"
        if settings.secret_key == _DEFAULT_KEY:
            raise RuntimeError(
                "SECRET_KEY está com valor padrão inseguro. "
                "Defina a variável de ambiente SECRET_KEY com uma string aleatória de ≥ 64 chars."
            )
        if not settings.stripe_webhook_secret:
            raise RuntimeError(
                "STRIPE_WEBHOOK_SECRET obrigatório em produção. "
                "Configure a variável de ambiente STRIPE_WEBHOOK_SECRET."
            )
        logger.info("✅ Validações de startup (production) OK")

    await init_db()
    yield


app = FastAPI(
    title="CaseOS API",
    description="Relato de Caso Clínico Científico — Backend",
    version="1.1.0",
    lifespan=lifespan,
    # Desabilita docs em produção para não expor schema da API
    docs_url=None if settings.environment == "production" else "/docs",
    redoc_url=None if settings.environment == "production" else "/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
_ALLOWED_ORIGINS = [
    settings.frontend_url,
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    # "null" REMOVIDO — abria vetor CSRF via iframes e file://
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)

app.include_router(artigo.router)
app.include_router(auth.router)
app.include_router(webhooks.router)
app.include_router(chat.router)
app.include_router(imagens.router)


@app.websocket("/ws/{sessao_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    sessao_id: str,
    token: Optional[str] = Query(default=None),
):
    # Token obrigatório — WebSocket não suporta Authorization header,
    # então é passado via query param ?token=...
    if not token:
        await websocket.close(code=4001, reason="Token de autenticação ausente.")
        return
    try:
        decode_token(token, "access")
    except Exception:
        await websocket.close(code=4001, reason="Token inválido ou expirado.")
        return

    await manager.connect(sessao_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(sessao_id)


@app.get("/health")
async def health():
    """Endpoint de health check para monitoramento externo."""
    from datetime import datetime, timezone
    from sqlalchemy import text
    db_ok = False
    try:
        from app.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "version": "1.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "api": "ok",
            "database": "ok" if db_ok else "error",
        },
    }
