"""reminder_marks, task_approvals.notified_at: проактивные напоминания

До сих пор бот говорил только в ответ: на команду, на нажатие, на событие
портала. Три вещи от этого молчали.

* Запрос на подтверждение задачи мог не доставиться (человек ни разу не открывал
  диалог с ботом — Telegram отвечает 400/403), и об этом не узнавал никто:
  задача висела `pending`, а `/pending` надо ещё догадаться набрать. Колонка
  `notified_at` делает недоставку видимой: NULL значит «в личку не дошло», и
  через полчаса такой запрос уходит в чат проекта словами.
* Подтверждение никто не торопил: ни напоминания, ни эскалации.
* Срок задачи наступал молча, а утренней сводки не было вовсе.

`reminder_marks` — отметки «это уже отправлено», одна таблица на все три вида.
Без неё каждый проход воркера слал бы напоминание заново: условие («срок через
два часа», «висит сутки») остаётся истинным всё время, пока длится окно.

**Отметка ставится ДО отправки, а не после.** Направление выбрано осознанно:
худшее, что бывает при отметке до, — одно потерянное напоминание; худшее при
отметке после — бесконечный поток одинаковых сообщений человеку, пока Telegram
отвечает ошибкой. Сообщения в чат этим не рискуют вовсе: они идут через `outbox`,
у которой свои ретраи.

`fingerprint` отличает «то же самое» от «изменилось»: у напоминания о сроке это
сам срок (перенесли — напомним снова), у дайджеста — местная дата чата (одна
сводка в сутки), у подтверждения он пуст (напоминание одно на запрос).

Revision ID: 0015_reminders
Revises: 0014_outbox_markup
"""
from alembic import op

revision = "0015_reminders"
down_revision = "0014_outbox_markup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE task_approvals ADD COLUMN notified_at TIMESTAMPTZ")
    op.execute("""
        COMMENT ON COLUMN task_approvals.notified_at IS
          'Когда запрос доставлен в личку ответственному. NULL = не доставлен ни разу:
           человек не открывал диалог с ботом, и запрос надо эскалировать в чат,
           а не ждать молча'
    """)

    op.execute("""
        CREATE TABLE reminder_marks (
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          scope       TEXT        NOT NULL CHECK (scope IN ('approval','task','chat')),
          scope_id    BIGINT      NOT NULL,
          kind        TEXT        NOT NULL,
          fingerprint TEXT        NOT NULL DEFAULT '',
          sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, scope, scope_id, kind)
        )
    """)
    op.execute("""
        COMMENT ON TABLE reminder_marks IS
          'Что уже напомнено. Условие напоминания истинно всё окно целиком, поэтому
           без отметки каждый проход воркера слал бы его заново (domain/reminders.py)'
    """)
    op.execute("""
        COMMENT ON COLUMN reminder_marks.fingerprint IS
          'Отпечаток повода: срок задачи, местная дата чата. Сменился — повод новый,
           напоминание уходит снова; совпал — молчим'
    """)
    # Ретенция: воркер чистит отметки старше REMINDER_MARK_RETENTION.
    op.execute("CREATE INDEX ix_reminder_marks__sent ON reminder_marks (sent_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reminder_marks")
    op.execute("ALTER TABLE task_approvals DROP COLUMN IF EXISTS notified_at")
