"""bot runtime: кнопки, идемпотентность мутаций, связь сообщений с задачами

Revision ID: 0004_bot_runtime
Revises: 0003_polling
"""
from alembic import op

revision = "0004_bot_runtime"
down_revision = "0003_polling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Наружу уходит случайное значение, в БД лежит его sha256. Последовательный
    # идентификатор здесь означал бы, что чужую привязку можно завершить перебором
    # соседних значений (docs/40-security.md §8).
    op.execute("""
        CREATE TABLE callback_tokens (
          token_hash  BYTEA       PRIMARY KEY,
          tenant_id   BIGINT      REFERENCES tenants(id) ON DELETE CASCADE,
          kind        TEXT        NOT NULL,
          owner_tg_id BIGINT,
          chat_ref    BIGINT      REFERENCES tg_chats(id) ON DELETE CASCADE,
          payload     JSONB       NOT NULL DEFAULT '{}',
          single_use  BOOLEAN     NOT NULL DEFAULT true,
          used_at     TIMESTAMPTZ,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at  TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_admin_owner
            CHECK (kind NOT LIKE 'admin:%' OR owner_tg_id IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX ix_callback_tokens__gc ON callback_tokens (expires_at)")

    # Инвариант И-10. Закрывает самый опасный класс: задача создана, ответ не дошёл
    # по таймауту, ретрай плодит дубль. Перед мутацией пишем idem_key, при повторе
    # сначала ищем уже созданный объект по нему.
    op.execute("""
        CREATE TABLE entity_external_refs (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          idem_key    TEXT        NOT NULL,
          source_kind TEXT        NOT NULL,
          source_key  TEXT        NOT NULL,
          target_kind TEXT        NOT NULL,
          target_id   BIGINT,
          state       TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','committed','failed')),
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, idem_key)
        )
    """)
    op.execute("CREATE INDEX ix_ext_refs__source "
               "ON entity_external_refs (tenant_id, source_kind, source_key)")

    op.execute("""
        CREATE TABLE tg_message_links (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          chat_ref    BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
          message_id  BIGINT      NOT NULL,
          kind        TEXT        NOT NULL
                        CHECK (kind IN ('source','card','draft','notify')),
          b24_task_id BIGINT,
          render_hash TEXT,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (chat_ref, message_id, kind)
        )
    """)
    op.execute("CREATE INDEX ix_tg_message_links__task "
               "ON tg_message_links (tenant_id, b24_task_id) WHERE b24_task_id IS NOT NULL")


    # Кэш задач. Событие Битрикса не содержит diff — только ID (проверено, §11.1
    # docs/00-portal-facts.md). Поэтому кэш не оптимизация, а обязательная подсистема:
    # без него нельзя понять, что изменилось, и нельзя сообщить об удалении задачи.
    op.execute("""
        CREATE TABLE task_cache (
          tenant_id       BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          b24_task_id     BIGINT      NOT NULL,
          project_id      BIGINT      REFERENCES projects(id) ON DELETE SET NULL,
          b24_group_id    BIGINT,
          is_ours         BOOLEAN     NOT NULL DEFAULT true,
          title           TEXT,
          status          SMALLINT,
          sub_status      SMALLINT,
          stage_id        BIGINT,
          responsible_id  BIGINT,
          created_by      BIGINT,
          priority        SMALLINT,
          deadline        TIMESTAMPTZ,
          created_date    TIMESTAMPTZ,
          changed_date    TIMESTAMPTZ,
          closed_date     TIMESTAMPTZ,
          overdue_notified_at TIMESTAMPTZ,
          synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at      TIMESTAMPTZ,
          PRIMARY KEY (tenant_id, b24_task_id)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN task_cache.is_ours IS
          'false = «отбойник»: проверено, задача не из наших проектов. Подписка на события
           не фильтруется по группе, приходят события ВСЕХ задач портала; без отбойников
           каждое неизвестное ID стоит дозапроса и выедает квоту клиента. Строка хранит
           только идентификатор и флаг, без заголовка и полей'
    """)
    op.execute("CREATE INDEX ix_task_cache__project_open ON task_cache "
               "(tenant_id, project_id, status) WHERE is_ours AND status <> 5")
    op.execute("CREATE INDEX ix_task_cache__overdue ON task_cache "
               "(tenant_id, project_id, deadline) "
               "WHERE is_ours AND status <> 5 AND deadline IS NOT NULL")
    op.execute("CREATE INDEX ix_task_cache__route ON task_cache (tenant_id, b24_group_id)")
    op.execute("ALTER TABLE task_cache SET (fillfactor = 85, "
               "autovacuum_vacuum_scale_factor = 0.05)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_cache")
    op.execute("DROP TABLE IF EXISTS tg_message_links")
    op.execute("DROP TABLE IF EXISTS entity_external_refs")
    op.execute("DROP TABLE IF EXISTS callback_tokens")
