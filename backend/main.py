from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional

from app.config import settings
from app.models.database import init_db
from app.services.websocket_manager import manager
from app.services.auth import decode_token
from app.routers import artigo, auth, webhooks, chat


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="RCCS API",
    description="Relato de Caso Clínico Científico — Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url,
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "null",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(artigo.router)
app.include_router(auth.router)
app.include_router(webhooks.router)
app.include_router(chat.router)


@app.websocket("/ws/{sessao_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    sessao_id: str,
    token: Optional[str] = Query(default=None),
):
    # Autenticação via query param (WebSocket não suporta Authorization header)
    if token:
        try:
            decode_token(token, "access")
        except Exception:
            await websocket.close(code=4001)
            return

    await manager.connect(sessao_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(sessao_id)


@app.get("/health")
async def health():
    """Endpoint de health check para monitoramento externo (UptimeRobot, BetterStack etc)."""
    from datetime import datetime, timezone
    from sqlalchemy import text
    from app.models.database import get_db
    db_ok = False
    try:
        from app.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "api": "ok",
            "database": "ok" if db_ok else "error",
        }
    }
