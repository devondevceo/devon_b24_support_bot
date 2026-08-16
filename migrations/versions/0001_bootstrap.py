"""bootstrap: теннанты, per-user токены, журнал payload для SPIKE A

Это минимальный набор таблиц, нужный, чтобы принять установку приложения и получить
первый живой per-user токен. Полная схема из docs/20-data-model.md накатывается
следующими миграциями ПОСЛЕ спайка — часть решений зависит от того, что реально
пришлёт портал.

Правило вперёд-совместимости: здесь заводятся только те колонки, в которых мы уверены;
остальные добавляются отдельными миграциями, а не переопределением этих.

Revision ID: 0001_bootstrap
Revises:
"""
from alembic import op

revision = "0001_bootstrap"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute("""
        CREATE DOMAIN enc_text AS TEXT
          CHECK (VALUE ~ '^enc:[0-9]+:[0-9]+:[A-Za-z0-9_-]+$')
    """)

    op.execute("""
        CREATE TABLE tenants (
          id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          slug                  TEXT        NOT NULL,
          name                  TEXT        NOT NULL,
          status                TEXT        NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active','suspended','uninstalled','deleted')),
          b24_member_id         TEXT        NOT NULL UNIQUE,
          b24_domain            TEXT        NOT NULL,
          b24_app_token         enc_text,
          b24_app_token_kid     SMALLINT,
          granted_scope         TEXT[]      NOT NULL DEFAULT '{}',
          install_state         TEXT        NOT NULL DEFAULT 'pending'
                                  CHECK (install_state IN ('pending','installed','finished','broken')),
          tz                    TEXT        NOT NULL DEFAULT 'Europe/Moscow',
          settings              JSONB       NOT NULL DEFAULT '{}',
          created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        COMMENT ON COLUMN tenants.b24_app_token IS
          'APPLICATION_TOKEN. Идентификатор портала, НЕ доказательство подлинности:
           его видит любой сотрудник портала через F12 (инвариант И-8)'
    """)

    op.execute("""
        CREATE TABLE b24_user_tokens (
          tenant_id             BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          b24_user_id           BIGINT      NOT NULL,
          role                  TEXT        NOT NULL DEFAULT 'user'
                                  CHECK (role IN ('user','service_admin','service_reader')),
          authorized_tg_user_id BIGINT,
          access_token          enc_text    NOT NULL,
          refresh_token         enc_text    NOT NULL,
          enc_kid               SMALLINT    NOT NULL,
          expires_at            TIMESTAMPTZ NOT NULL,
          token_version         INT         NOT NULL DEFAULT 1,
          state                 TEXT        NOT NULL DEFAULT 'active'
                                  CHECK (state IN ('active','needs_reauth','revoked')),
          last_refresh_at       TIMESTAMPTZ,
          last_used_at          TIMESTAMPTZ,
          PRIMARY KEY (tenant_id, b24_user_id)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN b24_user_tokens.authorized_tg_user_id IS
          'Кто именно авторизовал этот токен. Без сверки с действующим пользователем
           привязка по e-mail отдаёт доступ к чужому токену (см. docs/40-security.md §2)'
    """)
    op.execute("CREATE INDEX ix_b24_user_tokens__warmup ON b24_user_tokens (last_refresh_at) "
               "WHERE state = 'active'")
    op.execute("CREATE INDEX ix_b24_user_tokens__rekey ON b24_user_tokens (enc_kid)")

    # Журнал структуры входящих payload. Только для спайков: значения секретов
    # маскируются в приложении до записи. Удаляется после SPIKE A и SPIKE E.
    op.execute("""
        CREATE TABLE b24_payload_log (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          kind        TEXT        NOT NULL,
          member_id   TEXT,
          shape       JSONB       NOT NULL,
          received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_b24_payload_log__kind ON b24_payload_log (kind, received_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS b24_payload_log")
    op.execute("DROP TABLE IF EXISTS b24_user_tokens")
    op.execute("DROP TABLE IF EXISTS tenants")
    op.execute("DROP DOMAIN IF EXISTS enc_text")
