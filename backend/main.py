from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from typing import Optional
import logging

from app.config import settings
from app.models.database import init_db
from app.services.websocket_manager import manager
from app.services.auth import decode_token
from app.routers import artigo, auth, webhooks, chat, imagens, admin, revisoes, notificacoes

logger = logging.getLogger(__name__)

# ── Limites de payload ────────────────────────────────────────────────────────
# JSON API: 512 KB cobre qualquer payload válido (CKO completo ≈ 30-80 KB).
# Uploads multipart: 15 MB — cobre imagens (≤5 MB) e PDFs do Review Studio (≤12 MB).
_MAX_BODY_BYTES   = 512 * 1024        # 512 KB — endpoints JSON
_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB  — multipart/form-data (imagens + PDFs)


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

    # ── Auto-promoção do admin ────────────────────────────────────────────────
    # Se ADMIN_EMAIL estiver configurado, garante is_admin=True no BD.
    # Funciona no primeiro boot e após resets manuais.
    if settings.admin_email:
        try:
            from app.models.database import AsyncSessionLocal, Usuario
            from sqlalchemy import select as _select
            async with AsyncSessionLocal() as _db:
                _res = await _db.execute(
                    _select(Usuario).where(Usuario.email == settings.admin_email)
                )
                _admin_user = _res.scalar_one_or_none()
                if _admin_user and not _admin_user.is_admin:
                    _admin_user.is_admin = True
                    await _db.commit()
                    logger.info("✅ Admin promovido: %s", settings.admin_email)
                elif _admin_user:
                    logger.info("✅ Admin já configurado: %s", settings.admin_email)
                else:
                    logger.warning(
                        "⚠️  ADMIN_EMAIL=%s não encontrado no banco ainda. "
                        "Crie a conta e reinicie para promover.",
                        settings.admin_email,
                    )
        except Exception as e:
            logger.error("Erro ao promover admin: %s", e)

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
    settings.frontend_url,                      # FRONTEND_URL no Render
    "https://caseos.voandonaia.com",
    "https://reviewstudio.voandonaia.com",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    # "null" REMOVIDO — abria vetor CSRF via iframes e file://
]

# Origens extras via env var (ex: domínios Vercel gerados automaticamente)
if settings.extra_cors_origins:
    _ALLOWED_ORIGINS += [
        o.strip() for o in settings.extra_cors_origins.split(",")
        if o.strip().startswith("http")
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """
    Rejeita requests cujo Content-Length exceda o limite da rota:
      • multipart/form-data (uploads de imagem e PDF) → 15 MB
      • qualquer outro Content-Type (JSON API)         → 512 KB

    Nota: Content-Length ausente (chunked) passa pelo middleware, mas os
    endpoints de upload lêem o arquivo em chunks e aplicam seu próprio limite
    em bytes lidos (imagens.py / revisoes.py).
    """
    content_type = request.headers.get("content-type", "")
    is_upload = "multipart/form-data" in content_type
    limit = _MAX_UPLOAD_BYTES if is_upload else _MAX_BODY_BYTES

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            size = int(content_length)
        except ValueError:
            return JSONResponse({"detail": "Content-Length inválido."}, status_code=400)
        if size > limit:
            limit_str = f"{limit // (1024 * 1024)} MB" if is_upload else f"{limit // 1024} KB"
            return JSONResponse(
                {"detail": f"Payload excede o limite de {limit_str}."},
                status_code=413,
            )
    return await call_next(request)

app.include_router(artigo.router)
app.include_router(auth.router)
app.include_router(webhooks.router)
app.include_router(chat.router)
app.include_router(imagens.router)
app.include_router(admin.router)
app.include_router(revisoes.router)
app.include_router(notificacoes.router)


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
