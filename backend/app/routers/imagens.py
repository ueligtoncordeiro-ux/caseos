"""Upload de imagens clínicas associadas a uma sessão."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.models.database import Usuario
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


@router.post("/{sessao_id}")
async def upload_imagem(
    sessao_id: str,
    arquivo: UploadFile = File(...),
    numero_figura: int = Form(...),
    legenda: str = Form(...),
    titulo_abrev: str = Form(""),
    _user: Usuario = Depends(get_verified_user),   # ← autenticação obrigatória
):
    ext = Path(arquivo.filename or "img.jpg").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Tipo não suportado: {ext}")

    sessao_dir = _safe_path(IMAGES_DIR, sessao_id)
    sessao_dir.mkdir(parents=True, exist_ok=True)

    filename = f"figura_{numero_figura}{ext}"
    filepath = _safe_path(IMAGES_DIR, sessao_id, filename)

    content = await arquivo.read()
    filepath.write_bytes(content)
    logger.info("Imagem salva: %s (user=%s)", filepath, _user.id)

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
    _user: Usuario = Depends(get_verified_user),   # ← imagens clínicas são privadas
):
    filepath = _safe_path(IMAGES_DIR, sessao_id, filename)
    if not filepath.exists():
        raise HTTPException(404, "Imagem não encontrada")
    return FileResponse(str(filepath))


@router.delete("/{sessao_id}/{filename}")
async def delete_imagem(
    sessao_id: str,
    filename: str,
    _user: Usuario = Depends(get_verified_user),   # ← autenticação obrigatória
):
    filepath = _safe_path(IMAGES_DIR, sessao_id, filename)
    if filepath.exists():
        filepath.unlink()
    return {"deleted": True}
