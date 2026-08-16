"""notifications: входящие события, подавление эха, настройки, очередь отправки

Revision ID: 0006_notifications
Revises: 0005_fix_binding_trigger
"""
from alembic import op

revision = "0006_notifications"
down_revision = "0005_fix_binding_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Приём событий. Отвечаем 200 сразу, обработка асинхронная: Битрикс не должен
    # ждать, пока мы сходим за деталями задачи.
    op.execute("""
        CREATE TABLE b24_event_inbox (
          id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          event        TEXT        NOT NULL,
          b24_task_id  BIGINT,
          b24_user_id  BIGINT,
          dedup_key    TEXT        NOT NULL,
          state        TEXT        NOT NULL DEFAULT 'pending'
                         CHECK (state IN ('pending','processing','done','failed','dropped')),
          attempts     SMALLINT    NOT NULL DEFAULT 0,
          last_error   TEXT,
          received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          processed_at TIMESTAMPTZ,
          UNIQUE (tenant_id, dedup_key)
        )
    """)
    op.execute("CREATE INDEX ix_event_inbox__pending ON b24_event_inbox (received_at) "
               "WHERE state = 'pending'")
    op.execute("""
        COMMENT ON COLUMN b24_event_inbox.b24_user_id IS
          'auth[user_id] из события. У СИСТЕМНЫХ сообщений в чате задачи он пустой —
           это единственный надёжный признак, чтобы не слать в чат «новый комментарий»
           на каждое изменение задачи (docs/00-portal-facts.md §11.2)'
    """)

    # Подавление эха: своё же изменение не должно вернуться уведомлением.
    op.execute("""
        CREATE TABLE b24_echo_suppress (
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          b24_task_id BIGINT      NOT NULL,
          fingerprint TEXT        NOT NULL,
          expires_at  TIMESTAMPTZ NOT NULL,
          used_at     TIMESTAMPTZ,
          PRIMARY KEY (tenant_id, b24_task_id, fingerprint)
        )
    """)
    op.execute("""
        COMMENT ON TABLE b24_echo_suppress IS
          'fingerprint = sha256(поле|новое значение|автор). Гасим ОДНОКРАТНО и только
           точное совпадение: схема со слиянием наборов полей позволяла держать задачу
           «немой» в чате, повторяя безобидную правку раз в минуту'
    """)

    # Настройки уведомлений с наследованием. Отсутствие записи = наследовать выше,
    # запись = явное решение (инвариант И-9).
    op.execute("""
        CREATE TABLE notification_settings (
          tenant_id  BIGINT  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          scope_kind TEXT    NOT NULL CHECK (scope_kind IN ('tenant','project','binding')),
          scope_id   BIGINT  NOT NULL DEFAULT 0,
          code       TEXT    NOT NULL,
          enabled    BOOLEAN NOT NULL,
          PRIMARY KEY (tenant_id, scope_kind, scope_id, code)
        )
    """)

    # Исходящая очередь: переживает рестарт и держит лимит Telegram на группу.
    op.execute("""
        CREATE TABLE outbox (
          id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id       BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          bot_ref         BIGINT      NOT NULL REFERENCES tg_bots(id) ON DELETE CASCADE,
          chat_ref        BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
          thread_id       BIGINT,
          kind            TEXT        NOT NULL,
          text            TEXT        NOT NULL,
          dedup_key       TEXT,
          state           TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','sending','sent','failed','cancelled')),
          attempts        SMALLINT    NOT NULL DEFAULT 0,
          next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_error      TEXT,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          sent_at         TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX ix_outbox__ready ON outbox (next_attempt_at) "
               "WHERE state = 'pending'")
    op.execute("CREATE UNIQUE INDEX ux_outbox__dedup ON outbox (tenant_id, dedup_key) "
               "WHERE dedup_key IS NOT NULL AND state IN ('pending','sending')")
    op.execute("""
        COMMENT ON COLUMN outbox.text IS
          'Готовый текст уведомления. Переписки из Telegram здесь нет и быть не может:
           инвариант И-1 запрещает класть в БД содержимое чужих сообщений'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outbox")
    op.execute("DROP TABLE IF EXISTS notification_settings")
    op.execute("DROP TABLE IF EXISTS b24_echo_suppress")
    op.execute("DROP TABLE IF EXISTS b24_event_inbox")
