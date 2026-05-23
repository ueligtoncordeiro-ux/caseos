"""Upload de imagens clínicas associadas a uma sessão."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import Usuario, Sessao, get_db
from app.services.auth import get_verified_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/imagens", tags=["imagens"])
IMAGES_DIR = Path("images_output")
ALLOWED = {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp", ".webp"}


def _safe_path(base: Path, *parts: str) -> Path:
    """Resolve caminho e garante que está dentro de base (evita path traversal)."""
    resolved = (base / Path(*parts)).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="Caminho inválido.")
    return resolved


async def _verificar_dono(sessao_id: str, user_id: str, db: AsyncSession) -> Sessao:
    """
    Busca a sessão e garante que pertence ao usuário autenticado.
    Lança 404 se não existir, 403 se o dono for outro.
    Chamada obrigatória em todo endpoint que lê/grava/deleta imagens de uma sessão.
    """
    result = await db.execute(select(Sessao).where(Sessao.external_id == sessao_id))
    sessao = result.scalar_one_or_none()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    if sessao.user_id != user_id:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    return sessao


@router.post("/{sessao_id}")
async def upload_imagem(
    sessao_id: str,
    arquivo: UploadFile = File(...),
    numero_figura: int = Form(...),
    legenda: str = Form(...),
    titulo_abrev: str = Form(""),
    user: Usuario = Depends(get_verified_user),
    db: AsyncSession = Depends(get_db),
):
    await _verificar_dono(sessao_id, user.id, db)

    ext = Path(arquivo.filename or "img.jpg").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Tipo não suportado: {ext}")

    sessao_dir = _safe_path(IMAGES_DIR, sessao_id)
    sessao_dir.mkdir(parents=True, exist_ok=True)

    filename = f"figura_{numero_figura}{ext}"
    filepath = _safe_path(IMAGES_DIR, sessao_id, filename)

    content = await arquivo.read()
    filepath.write_bytes(content)
    logger.info("Imagem salva: %s (user=%s)", filepath, user.id)

    return {
        "sessao_id": sessao_id,
        "numero_figura": numero_figura,
        "filename": filename,
        "legenda": legenda,
        "titulo_abrev": titulo_abrev or f"Figura {numero_figura}",
        "url": f"/imagens/{sessao_id}/{filename}",
    }


@router.get("/{sessao_id}/{filename}")
async def get_imagem(
    sessao_id: str,
    filename: str,
    user: Usuario = Depends(get_verified_user),
    db: AsyncSession = Depends(get_db),
):
    await _verificar_dono(sessao_id, user.id, db)

    filepath = _safe_path(IMAGES_DIR, sessao_id, filename)
    if not filepath.exists():
        raise HTTPException(404, "Imagem não encontrada")
    return FileResponse(str(filepath))


@router.delete("/{sessao_id}/{filename}")
async def delete_imagem(
    sessao_id: str,
    filename: str,
    user: Usuario = Depends(get_verified_user),
    db: AsyncSession = Depends(get_db),
):
    await _verificar_dono(sessao_id, user.id, db)

    filepath = _safe_path(IMAGES_DIR, sessao_id, filename)
    if filepath.exists():
        filepath.unlink()
    return {"deleted": True}
