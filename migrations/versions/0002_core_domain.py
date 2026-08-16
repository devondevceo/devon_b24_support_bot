"""core domain: люди, клиенты, проекты, стадии, боты, чаты, привязки

Ставится после того, как SPIKE A и SPIKE E сняли неизвестности по placement и событиям
(docs/00-portal-facts.md §10, §11). Имена — канонические из docs/20-data-model.md.

Revision ID: 0002_core_domain
Revises: 0001_bootstrap
"""
from alembic import op

revision = "0002_core_domain"
down_revision = "0001_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------- люди
    op.execute("""
        CREATE TABLE users (
          id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tg_user_id    BIGINT UNIQUE,
          tg_username   TEXT,
          display_name  TEXT,
          is_superadmin BOOLEAN     NOT NULL DEFAULT false,
          session_epoch INT         NOT NULL DEFAULT 1,
          first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        COMMENT ON COLUMN users.tg_username IS
          'Показываем, но НИКОГДА не используем как ключ: username в Telegram передаваем,
           освободившийся @ivan может занять посторонний'
    """)
    op.execute("""
        COMMENT ON COLUMN users.session_epoch IS
          'Инкремент обесценивает все выданные сессии пользователя разом'
    """)

    op.execute("""
        CREATE TABLE tenant_members (
          tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          role         TEXT   NOT NULL DEFAULT 'member'
                         CHECK (role IN ('tenant_admin','member')),
          b24_user_id  BIGINT,
          link_status  TEXT   NOT NULL DEFAULT 'none'
                         CHECK (link_status IN ('none','matched','authorized',
                                                'needs_reauth','revoked')),
          linked_at    TIMESTAMPTZ,
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, user_id)
        )
    """)
    op.execute("CREATE INDEX ix_tenant_members__b24_user "
               "ON tenant_members (tenant_id, b24_user_id) WHERE b24_user_id IS NOT NULL")
    op.execute("""
        COMMENT ON COLUMN tenant_members.link_status IS
          'matched = знаем, кто это в Битриксе (только чтение);
           authorized = есть живой личный токен (создание и редактирование)'
    """)

    # ------------------------------------------------- клиенты и их проекты
    op.execute("""
        CREATE TABLE clients (
          id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id      BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          name           TEXT        NOT NULL,
          crm_company_id BIGINT,
          status         TEXT        NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','archived')),
          tz             TEXT,
          settings       JSONB       NOT NULL DEFAULT '{}',
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, name)
        )
    """)

    op.execute("""
        CREATE TABLE projects (
          id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id         BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          client_id         BIGINT      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
          b24_group_id      BIGINT      NOT NULL,
          kind              TEXT        NOT NULL DEFAULT 'workgroup'
                              CHECK (kind IN ('workgroup','project','collab')),
          name              TEXT        NOT NULL,
          is_extranet       BOOLEAN     NOT NULL DEFAULT false,
          owner_b24_user_id BIGINT,
          status            TEXT        NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','archived','deleted')),
          name_synced_at    TIMESTAMPTZ,
          members_synced_at TIMESTAMPTZ,
          stages_synced_at  TIMESTAMPTZ,
          defaults          JSONB       NOT NULL DEFAULT '{}',
          created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, b24_group_id)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN projects.status IS
          'archived/deleted проставляет суточный джоб: группу переименовали, закрыли
           или удалили. Архивный проект исчезает из кнопок выбора, но карточки его
           задач продолжают открываться из кэша'
    """)

    # Стадии канбана: то, что пользователь видит как «Новые», это стадия, а не статус.
    # У каждого проекта свои. Источник — task.stages.get(entityId=<b24_group_id>).
    op.execute("""
        CREATE TABLE project_stages (
          tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          project_id   BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          b24_stage_id BIGINT      NOT NULL,
          title        TEXT        NOT NULL,
          sort         INT         NOT NULL DEFAULT 0,
          system_type  TEXT,
          color        TEXT,
          synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, project_id, b24_stage_id)
        )
    """)

    # ----------------------------------------------------------- боты Telegram
    op.execute("""
        CREATE TABLE tg_bots (
          id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id          BIGINT      NOT NULL UNIQUE
                               REFERENCES tenants(id) ON DELETE CASCADE,
          bot_id             BIGINT      NOT NULL,
          username           TEXT        NOT NULL,
          token              enc_text    NOT NULL,
          token_kid          SMALLINT    NOT NULL,
          webhook_id         UUID        NOT NULL DEFAULT gen_random_uuid(),
          webhook_secret     enc_text    NOT NULL,
          webhook_secret_kid SMALLINT    NOT NULL,
          mode               TEXT        NOT NULL DEFAULT 'webhook'
                               CHECK (mode IN ('webhook','polling')),
          proxy_url          TEXT,
          status             TEXT        NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','active','error','suspended')),
          privacy_mode_off   BOOLEAN,
          last_check_at      TIMESTAMPTZ,
          last_error         TEXT,
          created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # ГЛОБАЛЬНЫЙ, не в пределах теннанта. Иначе админ теннанта B, заполучив токен бота
    # теннанта A, вставляет его себе, наш reconcile делает setWebhook на свой адрес —
    # и вся переписка чатов клиентов A уходит к B (docs/40-security.md §4).
    op.execute("CREATE UNIQUE INDEX ux_tg_bots__bot_id ON tg_bots (bot_id)")
    op.execute("CREATE UNIQUE INDEX ux_tg_bots__webhook_id ON tg_bots (webhook_id)")

    # -------------------------------------------------------- чаты и привязки
    op.execute("""
        CREATE TABLE tg_chats (
          id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          chat_id       BIGINT      NOT NULL,
          tenant_id     BIGINT      REFERENCES tenants(id) ON DELETE CASCADE,
          bot_ref       BIGINT      REFERENCES tg_bots(id) ON DELETE SET NULL,
          type          TEXT        NOT NULL DEFAULT 'group',
          title         TEXT,
          is_forum      BOOLEAN     NOT NULL DEFAULT false,
          status        TEXT        NOT NULL DEFAULT 'unclaimed'
                          CHECK (status IN ('unclaimed','claimed','active','left','migrated')),
          migrated_to   BIGINT REFERENCES tg_chats(id),
          first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          claimed_at    TIMESTAMPTZ
        )
    """)
    op.execute("CREATE UNIQUE INDEX ux_tg_chats__chat_id ON tg_chats (chat_id) "
               "WHERE status <> 'migrated'")
    op.execute("""
        COMMENT ON COLUMN tg_chats.id IS
          'Суррогатный ключ. ВСЕ прочие таблицы ссылаются на него, а не на телеграмный
           chat_id — тогда миграция группы в супергруппу это один UPDATE'
    """)

    op.execute("""
        CREATE TABLE tg_topics (
          id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id       BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          chat_ref        BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
          thread_id       BIGINT      NOT NULL,
          name            TEXT,
          is_closed       BOOLEAN     NOT NULL DEFAULT false,
          is_deleted      BOOLEAN     NOT NULL DEFAULT false,
          name_updated_at TIMESTAMPTZ,
          UNIQUE (chat_ref, thread_id)
        )
    """)

    op.execute("""
        CREATE TABLE chat_bindings (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          chat_ref    BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
          topic_ref   BIGINT      REFERENCES tg_topics(id) ON DELETE CASCADE,
          project_id  BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          status      TEXT        NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','broken','disabled')),
          created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX ux_chat_bindings__uniq "
               "ON chat_bindings (chat_ref, COALESCE(topic_ref, 0), project_id)")
    op.execute("CREATE INDEX ix_chat_bindings__project "
               "ON chat_bindings (tenant_id, project_id) WHERE status = 'active'")

    # Инвариант: все проекты, привязанные к одному чату, принадлежат ОДНОМУ клиенту.
    # Это прямое требование заказчика и одновременно граница изоляции между клиентами.
    op.execute("""
        CREATE FUNCTION chat_bindings_one_client() RETURNS trigger AS $$
        DECLARE
          new_client BIGINT;
          other_client BIGINT;
        BEGIN
          SELECT client_id INTO new_client FROM projects WHERE id = NEW.project_id;
          SELECT p.client_id INTO other_client
            FROM chat_bindings b JOIN projects p ON p.id = b.project_id
           WHERE b.chat_ref = NEW.chat_ref
             AND b.id <> COALESCE(NEW.id, -1)
             AND b.status = 'active'
             AND p.client_id <> new_client
           LIMIT 1;
          IF other_client IS NOT NULL THEN
            RAISE EXCEPTION 'chat % already bound to another client (%), refusing %',
              NEW.chat_ref, other_client, new_client;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_chat_bindings_one_client
        BEFORE INSERT OR UPDATE ON chat_bindings
        FOR EACH ROW EXECUTE FUNCTION chat_bindings_one_client()
    """)

    # ------------------------------------------- сессии страниц внутри Б24
    # Страница приложения рендерится по POST от портала. Чтобы форма, которую она
    # отправляет обратно, была привязана к конкретному пользователю, выдаём
    # короткоживущий токен: наружу случайное значение, в БД его sha256.
    op.execute("""
        CREATE TABLE app_sessions (
          token_hash   BYTEA       PRIMARY KEY,
          tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          b24_user_id  BIGINT      NOT NULL,
          is_portal_admin BOOLEAN  NOT NULL DEFAULT false,
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at   TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_app_sessions__gc ON app_sessions (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_sessions")
    op.execute("DROP TRIGGER IF EXISTS trg_chat_bindings_one_client ON chat_bindings")
    op.execute("DROP FUNCTION IF EXISTS chat_bindings_one_client()")
    op.execute("DROP TABLE IF EXISTS chat_bindings")
    op.execute("DROP TABLE IF EXISTS tg_topics")
    op.execute("DROP TABLE IF EXISTS tg_chats")
    op.execute("DROP TABLE IF EXISTS tg_bots")
    op.execute("DROP TABLE IF EXISTS project_stages")
    op.execute("DROP TABLE IF EXISTS projects")
    op.execute("DROP TABLE IF EXISTS clients")
    op.execute("DROP TABLE IF EXISTS tenant_members")
    op.execute("DROP TABLE IF EXISTS users")
