import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, DateTime, JSON, Text, Boolean, Integer, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


# ── Planos disponíveis ────────────────────────────────────────────────────────
PLANO_FREE          = "free"
PLANO_PRO           = "pro"
PLANO_INSTITUCIONAL = "institucional"

QUOTA_MENSAL = {
    PLANO_FREE:          1,
    PLANO_PRO:           30,
    PLANO_INSTITUCIONAL: None,   # ilimitado
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

    # Plano e quota
    plano: Mapped[str] = mapped_column(String, default=PLANO_FREE)
    artigos_mes: Mapped[int] = mapped_column(Integer, default=0)
    mes_referencia: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # "2025-05"

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


# ── Engine & session factory ──────────────────────────────────────────────────

engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
