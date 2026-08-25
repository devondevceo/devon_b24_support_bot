"""notification_digest_settings, outbox.digest: группировка уведомлений

Вторая ручка настройки уведомлений рядом с уже существующей `notification_settings`:
та отвечает на вопрос «о чём сообщать», эта — «как часто». Отдельная таблица, а не
колонка в первой: там строка на КАЖДЫЙ код события, а интервал у уровня один, и
класть его восемь раз значило бы завести восемь мест, где он может разойтись.

Наследование то же самое и по тем же правилам (инвариант И-9):
`binding` → `project` → `tenant` → системный дефолт, отсутствие записи =
наследовать выше. `minutes = 0` — это явное «слать сразу», а не «не настроено».

`outbox.digest` помечает строку как накопительную: она ждёт своего окна и уезжает
вместе с соседками одним сообщением. `outbox.digest_text` — та же новость одной
строкой; собирается при постановке в очередь по той же причине, что и `markup`
(миграция `0014`): в момент отправки воркер уже не знает ни задачи, ни чата.

Revision ID: 0015_notification_digest
Revises: 0014_outbox_markup
"""
from alembic import op

revision = "0015_notification_digest"
down_revision = "0014_outbox_markup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE notification_digest_settings (
          tenant_id  BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          scope_kind TEXT        NOT NULL
                       CHECK (scope_kind IN ('tenant','project','binding')),
          scope_id   BIGINT      NOT NULL DEFAULT 0,
          minutes    SMALLINT    NOT NULL CHECK (minutes >= 0 AND minutes <= 1440),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, scope_kind, scope_id)
        )
    """)
    op.execute("""
        COMMENT ON TABLE notification_digest_settings IS
          'Интервал группировки уведомлений в минутах. Наследование как у
           notification_settings: binding -> project -> tenant -> 0 (сразу).
           Отсутствие строки = наследовать выше, minutes=0 = явное «слать сразу».
           Верхняя граница 1440 — сутки: окно длиннее означает не группировку,
           а тихое выключение уведомлений, для которого есть свои переключатели'
    """)

    op.execute("ALTER TABLE outbox ADD COLUMN digest BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE outbox ADD COLUMN digest_text TEXT")
    op.execute("""
        COMMENT ON COLUMN outbox.digest IS
          'Строка ждёт своего окна группировки и уедет вместе с соседками одним
           сообщением. next_attempt_at у таких строк — время отправки сводки,
           а не «можно повторить попытку»'
    """)
    op.execute("""
        COMMENT ON COLUMN outbox.digest_text IS
          'Та же новость одной строкой, для сводки. Собирается при постановке в
           очередь: в момент отправки воркер уже не знает ни задачи, ни чата.
           Переписки из Telegram здесь нет и быть не может (инвариант И-1)'
    """)
    # Выборка сводки идёт по «чат + тема + пора ли»: без частичного индекса это
    # полный проход по очереди на каждом тике воркера.
    op.execute("CREATE INDEX ix_outbox__digest ON outbox "
               "(tenant_id, chat_ref, thread_id, next_attempt_at) "
               "WHERE state = 'pending' AND digest")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_outbox__digest")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS digest_text")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS digest")
    op.execute("DROP TABLE IF EXISTS notification_digest_settings")
