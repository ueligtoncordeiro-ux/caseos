import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, DateTime, JSON, Text, Boolean, Integer, ForeignKey, LargeBinary, text
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

    # Timestamp da última troca de senha — usado para invalidar refresh tokens emitidos antes.
    # NULL = senha nunca trocada (todos os tokens válidos até expirar normalmente).
    pw_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

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
    revisoes: Mapped[List["Revisao"]] = relationship(
        "Revisao",
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

    # ── Versões DOCX (bytes armazenados no banco — sem depender de filesystem) ──
    # Original: gerado pelo pipeline, nunca é sobrescrito.
    # Editado: regenerado após cada edição manual do usuário.
    docx_original_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    docx_editado_bytes:  Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)

    # Diagnóstico de falha — preenchidos apenas quando status="erro"
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_stage:   Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Soft-delete — preenchido ao excluir; usuário não vê, admin vê para auditoria
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    versoes_docx: Mapped[List["VersaoDocx"]] = relationship(
        "VersaoDocx", back_populates="sessao", lazy="noload",
        order_by="VersaoDocx.numero",
        cascade="all, delete-orphan",
    )


class VersaoDocx(Base):
    __tablename__ = "versoes_docx"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    sessao_external_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessoes.external_id"), nullable=False, index=True
    )
    numero: Mapped[int] = mapped_column(Integer, nullable=False)
    docx_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    descricao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sessao: Mapped["Sessao"] = relationship("Sessao", back_populates="versoes_docx")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    acao: Mapped[str] = mapped_column(String, index=True)
    entidade_tipo: Mapped[str] = mapped_column(String, index=True)
    entidade_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    metadados: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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


# ── Review Studio — revisões científicas assistidas por IA ───────────────────

class Revisao(Base):
    __tablename__ = "revisoes"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("usuarios.id"),
                                         nullable=False, index=True)
    usuario: Mapped["Usuario"] = relationship("Usuario", back_populates="revisoes")

    tipo: Mapped[str] = mapped_column(String, default="narrativa")
    status: Mapped[str] = mapped_column(String, default="rascunho", index=True)
    titulo: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tema: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pergunta: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    objetivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mneumonico: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    guideline: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    formato_ref: Mapped[str] = mapped_column(String, default="vancouver")

    protocolo: Mapped[dict] = mapped_column(JSON, default=dict)
    criterios: Mapped[dict] = mapped_column(JSON, default=dict)
    preferencias: Mapped[dict] = mapped_column(JSON, default=dict)
    checklist: Mapped[dict] = mapped_column(JSON, default=dict)

    tokens_usados: Mapped[int] = mapped_column(Integer, default=0)
    docx_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime,
                                                  default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)

    fontes: Mapped[List["RevisaoFonte"]] = relationship(
        "RevisaoFonte",
        back_populates="revisao",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    matriz: Mapped[List["RevisaoMatrizItem"]] = relationship(
        "RevisaoMatrizItem",
        back_populates="revisao",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    documentos: Mapped[List["RevisaoDocumento"]] = relationship(
        "RevisaoDocumento",
        back_populates="revisao",
        lazy="noload",
        cascade="all, delete-orphan",
    )


class RevisaoFonte(Base):
    __tablename__ = "revisao_fontes"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    revisao_id: Mapped[str] = mapped_column(String, ForeignKey("revisoes.id"),
                                            nullable=False, index=True)
    revisao: Mapped["Revisao"] = relationship("Revisao", back_populates="fontes")

    origem: Mapped[str] = mapped_column(String, default="busca")
    fonte_base: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="importada", index=True)

    titulo: Mapped[str] = mapped_column(Text)
    autores: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ano: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    periodico: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    doi: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    pmid: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    abstract: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    arquivo_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadados: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    relevancia_ia: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    decisao_ia: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    decisao_humana: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    motivo_decisao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    aprovada_para_escrita: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime,
                                                  default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)

    chunks: Mapped[List["RevisaoFonteChunk"]] = relationship(
        "RevisaoFonteChunk",
        back_populates="fonte",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    decisoes: Mapped[List["RevisaoDecisao"]] = relationship(
        "RevisaoDecisao",
        back_populates="fonte",
        lazy="noload",
        cascade="all, delete-orphan",
    )


class RevisaoFonteChunk(Base):
    __tablename__ = "revisao_fonte_chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    fonte_id: Mapped[str] = mapped_column(String, ForeignKey("revisao_fontes.id"),
                                          nullable=False, index=True)
    fonte: Mapped["RevisaoFonte"] = relationship("RevisaoFonte", back_populates="chunks")

    ordem: Mapped[int] = mapped_column(Integer, default=0)
    secao: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    texto: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    metadados: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RevisaoDecisao(Base):
    __tablename__ = "revisao_decisoes"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    revisao_id: Mapped[str] = mapped_column(String, ForeignKey("revisoes.id"),
                                            nullable=False, index=True)
    fonte_id: Mapped[str] = mapped_column(String, ForeignKey("revisao_fontes.id"),
                                          nullable=False, index=True)
    fonte: Mapped["RevisaoFonte"] = relationship("RevisaoFonte", back_populates="decisoes")

    decisao: Mapped[str] = mapped_column(String)
    motivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    etapa: Mapped[str] = mapped_column(String, default="triagem")
    feita_por: Mapped[str] = mapped_column(String, default="humano")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RevisaoMatrizItem(Base):
    __tablename__ = "revisao_matriz"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    revisao_id: Mapped[str] = mapped_column(String, ForeignKey("revisoes.id"),
                                            nullable=False, index=True)
    revisao: Mapped["Revisao"] = relationship("Revisao", back_populates="matriz")
    fonte_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("revisao_fontes.id"),
                                                    nullable=True, index=True)

    autor_ano: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    objetivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metodo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    populacao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    principais_achados: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    limitacoes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    contribuicao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    secao_sugerida: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    qualidade: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    dados_extraidos: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime,
                                                  default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)


class RevisaoDocumento(Base):
    __tablename__ = "revisao_documentos"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    revisao_id: Mapped[str] = mapped_column(String, ForeignKey("revisoes.id"),
                                            nullable=False, index=True)
    revisao: Mapped["Revisao"] = relationship("Revisao", back_populates="documentos")

    tipo: Mapped[str] = mapped_column(String, default="manuscrito")
    versao: Mapped[int] = mapped_column(Integer, default=1)
    formato_ref: Mapped[str] = mapped_column(String, default="vancouver")
    secoes: Mapped[dict] = mapped_column(JSON, default=dict)
    referencias: Mapped[list] = mapped_column(JSON, default=list)
    citacoes: Mapped[dict] = mapped_column(JSON, default=dict)
    checklist: Mapped[dict] = mapped_column(JSON, default=dict)
    docx_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Notificações in-app ───────────────────────────────────────────────────────

NOTIF_TIPOS = {
    "artigo_concluido": "✅",
    "erro_pipeline":    "⚠️",
    "plano_atualizado": "🎉",
    "cota_esgotada":    "🔴",
    "bem_vindo":        "👋",
    "dica":             "💡",
}


class Notificacao(Base):
    __tablename__ = "notificacoes"

    id: Mapped[str] = mapped_column(String, primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("usuarios.id"),
                                         nullable=False, index=True)
    tipo: Mapped[str] = mapped_column(String, default="dica", index=True)
    titulo: Mapped[str] = mapped_column(String)
    texto: Mapped[str] = mapped_column(Text)
    lida: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    link: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # URL opcional para ação
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


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
            ("usuarios", "tokens_mes",    "INTEGER DEFAULT 0",       "INTEGER DEFAULT 0"),
            ("sessoes",  "tokens_usados", "INTEGER DEFAULT 0",       "INTEGER DEFAULT 0"),
            ("usuarios", "is_admin",      "BOOLEAN DEFAULT 0",       "BOOLEAN DEFAULT FALSE"),
            ("sessoes",  "error_message",       "TEXT",    "TEXT"),
            ("sessoes",  "error_stage",         "VARCHAR", "VARCHAR(100)"),
            ("sessoes",  "docx_original_bytes", "BLOB",    "BYTEA"),
            ("sessoes",  "docx_editado_bytes",  "BLOB",    "BYTEA"),
            ("sessoes",  "deleted_at",           "DATETIME", "TIMESTAMP"),
            ("usuarios", "pw_changed_at",         "DATETIME", "TIMESTAMP"),
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
