import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, DateTime, JSON, Text, Boolean, Integer, ForeignKey, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


# ── Planos disponíveis ────────────────────────────────────────────────────────
PLANO_FREE          = "free"
PLANO_STARTER       = "starter"
PLANO_PRO           = "pro"
PLANO_INSTITUCIONAL = "institucional"

QUOTA_MENSAL = {
    PLANO_FREE:          1,
    PLANO_STARTER:       6,
    PLANO_PRO:           12,
    PLANO_INSTITUCIONAL: None,   # ilimitado
}

# ── Limites de tokens por plano (mensais) ─────────────────────────────────────
# Alinhados com os price_ids do Stripe — 4 planos ativos
TOKENS_LIMITE = {
    PLANO_FREE:            50_000,   # só demos (~2 prévias)
    PLANO_STARTER:        150_000,   # ~6 artigos completos
    PLANO_PRO:            300_000,   # ~12 artigos completos
    PLANO_INSTITUCIONAL: 1_500_000,  # ~60 artigos
}

# ── Preços BRL (centavos) — sincronizados com Stripe ─────────────────────────
PRECO_BRL = {
    PLANO_FREE:              0,
    PLANO_STARTER:       4_900,  # R$49/mês
    PLANO_PRO:           9_900,  # R$99/mês
    PLANO_INSTITUCIONAL: 34_900, # R$349/mês
}


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    nome: Mapped[str] = mapped_column(String)

    # Autenticação local
    hashed_pw: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Google OAuth
    google_sub: Mapped[Optional[str]] = mapped_column(String, unique=True,
                                                    nullable=True, index=True)
    google_picture: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Plano e quota
    plano: Mapped[str] = mapped_column(String, default=PLANO_FREE)
    artigos_mes: Mapped[int] = mapped_column(Integer, default=0)
    mes_referencia: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # "2025-05"

    # Tokens LLM consumidos no mês corrente
    tokens_mes: Mapped[int] = mapped_column(Integer, default=0)

    # Stripe
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_subscription_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Perfil profissional (diferencial LGPD / CRM)
    crm_cro: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Consentimento LGPD
    lgpd_aceito_em: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime,
                                                  default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)

    sessoes: Mapped[List["Sessao"]] = relationship("Sessao", back_populates="usuario",
                                                    lazy="noload")
    pesquisas_salvas: Mapped[List["PesquisaSalva"]] = relationship(
        "PesquisaSalva",
        back_populates="usuario",
        lazy="noload",
    )


class Sessao(Base):
    __tablename__ = "sessoes"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    external_id: Mapped[str] = mapped_column(String, unique=True, index=True)

    # Vínculo com usuário (nullable para retrocompatibilidade)
    user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("usuarios.id"),
                                                  nullable=True, index=True)
    usuario: Mapped[Optional["Usuario"]] = relationship("Usuario", back_populates="sessoes")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime,
                                                  default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)
    status: Mapped[str] = mapped_column(String, default="rascunho")
    titulo: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cko: Mapped[dict] = mapped_column(JSON)
    resultado: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    relatorio: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    docx_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    care_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_usados: Mapped[int] = mapped_column(Integer, default=0)


class PesquisaSalva(Base):
    __tablename__ = "pesquisas_salvas"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("usuarios.id"),
                                         nullable=False, index=True)
    usuario: Mapped["Usuario"] = relationship("Usuario", back_populates="pesquisas_salvas")

    tipo: Mapped[str] = mapped_column(String, default="busca")
    query: Mapped[str] = mapped_column(Text)
    operador: Mapped[str] = mapped_column(String, default="AND")
    fontes: Mapped[list] = mapped_column(JSON, default=list)
    artigos: Mapped[list] = mapped_column(JSON, default=list)
    observacao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Engine & session factory ──────────────────────────────────────────────────

def _get_database_url() -> str:
    """
    Normaliza a DATABASE_URL para o driver async correto.
    Render/Heroku fornecem postgresql:// mas asyncpg precisa de postgresql+asyncpg://
    """
    url = settings.database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url

_db_url = _get_database_url()

engine = create_async_engine(
    _db_url,
    echo=settings.environment == "development",
    connect_args={"check_same_thread": False} if "sqlite" in _db_url else {},
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # ── Migração automática: colunas adicionadas incrementalmente ────────
        is_sqlite = "sqlite" in str(engine.url)
        migrations = [
            # (tabela, coluna, tipo_sqlite, tipo_pg)
            ("usuarios", "tokens_mes",   "INTEGER DEFAULT 0",       "INTEGER DEFAULT 0"),
            ("sessoes",  "tokens_usados","INTEGER DEFAULT 0",       "INTEGER DEFAULT 0"),
            ("usuarios", "is_admin",     "BOOLEAN DEFAULT 0",       "BOOLEAN DEFAULT FALSE"),
        ]
        for tabela, coluna, tipo_sqlite, tipo_pg in migrations:
            try:
                if is_sqlite:
                    await conn.execute(
                        text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo_sqlite}")
                    )
                else:
                    await conn.execute(
                        text(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS {coluna} {tipo_pg}")
                    )
            except Exception:
                pass  # coluna já existe — ignorar


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
