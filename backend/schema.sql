-- ──────────────────────────────────────────────────────────────────────────────
-- neuraxIA CaseOS — schema do banco de dados (SQLite / PostgreSQL compatível)
-- Gerado em: 2026-05-17
-- Este arquivo é a referência da estrutura do banco.
-- Para aplicar: sqlite3 rccs.db < schema.sql
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS usuarios (
    id VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    nome VARCHAR NOT NULL,
    hashed_pw VARCHAR,
    google_sub VARCHAR,
    google_picture TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    plano VARCHAR NOT NULL DEFAULT 'free',
    artigos_mes INTEGER NOT NULL DEFAULT 0,
    mes_referencia VARCHAR,
    stripe_customer_id VARCHAR,
    stripe_subscription_id VARCHAR,
    stripe_subscription_status VARCHAR,
    crm_cro VARCHAR,
    lgpd_aceito_em DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_usuarios_email ON usuarios (email);
CREATE UNIQUE INDEX IF NOT EXISTS ix_usuarios_google_sub ON usuarios (google_sub);

CREATE TABLE IF NOT EXISTS sessoes (
    id VARCHAR NOT NULL,
    external_id VARCHAR NOT NULL,
    user_id VARCHAR,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    cko JSON NOT NULL,
    resultado JSON,
    relatorio JSON,
    flags JSON NOT NULL DEFAULT '{}',
    docx_path TEXT,
    titulo VARCHAR,
    care_score INTEGER,
    PRIMARY KEY (id),
    FOREIGN KEY (user_id) REFERENCES usuarios (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_sessoes_external_id ON sessoes (external_id);
CREATE INDEX IF NOT EXISTS ix_sessoes_user_id ON sessoes (user_id);
