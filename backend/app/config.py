from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path
from dotenv import load_dotenv

# Carrega o .env explicitamente antes do pydantic (resolve problema com espaços no caminho)
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_FILE, override=True)


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_fallback_models: str = "gemini-flash-latest,gemini-2.0-flash-lite,gemini-2.0-flash"
    pubmed_api_key: str = ""
    semantic_scholar_api_key: str = ""
    polite_email: str = "caseos@rccs.com.br"  # identificação polite para OpenAlex/Crossref/Europe PMC
    resend_api_key: str = ""
    resend_from_email: str = "RCCS <noreply@rccs.com.br>"
    sherpa_romeo_api_key: str = ""  # disponível após julho/2026

    database_url: str = "sqlite+aiosqlite:///./rccs.db"

    secret_key: str = "dev-secret-key-troque-em-producao"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15    # 15 min — janela curta; frontend faz silent refresh

    environment: str = "development"
    frontend_url: str = "http://localhost:5500"
    backend_url: str = "http://localhost:8000"   # URL pública do backend (Render em prod)
    # Origens extras separadas por vírgula — use para adicionar domínios Vercel/preview
    # Ex: EXTRA_CORS_ORIGINS=https://caseos.vercel.app,https://caseos-abc123.vercel.app
    extra_cors_origins: str = ""
    log_level: str = "INFO"

    max_pipeline_timeout: int = 900
    max_agent_timeout: int = 120

    docx_output_dir: str = "./docx_output"
    review_upload_dir: str = "./review_uploads"

    # ── Google OAuth ──────────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_starter_price_id: str = "price_1TZxQv3oJrMmxd1m332GRzl2"
    stripe_pro_price_id: str = "price_1TZxR43oJrMmxd1m0BsTg21X"
    stripe_inst_price_id: str = "price_1TZxR73oJrMmxd1m1ZVKBsDS"

    # ── Admin ─────────────────────────────────────────────────────────────────
    # E-mail do administrador — promovido automaticamente a is_admin=True no startup.
    # Defina ADMIN_EMAIL no .env (Render). Nunca exponha em código-fonte.
    admin_email: str = ""

    # ── Token TTL ─────────────────────────────────────────────────────────────
    refresh_token_expire_days: int = 30

    class Config:
        env_file = str(_ENV_FILE)
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
