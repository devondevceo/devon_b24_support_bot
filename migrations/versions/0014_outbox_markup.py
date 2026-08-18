"""outbox.markup: кнопки под уведомлением

Уведомление из Битрикса — единственное сообщение бота, которое никто не вызывал
нажатием: его отправляет воркер из очереди. Поэтому клавиатура обязана храниться
вместе с текстом, а не собираться в момент отправки — воркер не знает ни задачи,
ни чата, ни прав, и собрать её ему не из чего.

В колонке лежат ТОЛЬКО токены кнопок и адрес портала. Полезная нагрузка кнопки
живёт в callback_tokens, как у всех остальных клавиатур (docs/30-bot-spec.md §0.2),
а переписки из Telegram здесь нет и быть не может — инвариант И-1 запрещает это
и для текста, и для всего остального в этой таблице.

Колонка nullable: уведомление без кнопок (например, об удалённой задаче) — это
нормальное состояние, а не отсутствующая настройка.

Revision ID: 0014_outbox_markup
Revises: 0013_task_approval
"""
from alembic import op

revision = "0014_outbox_markup"
down_revision = "0013_task_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE outbox ADD COLUMN markup JSONB")
    op.execute("""
        COMMENT ON COLUMN outbox.markup IS
          'reply_markup для sendMessage: токены кнопок и адрес портала, ничего больше.
           Собирается при постановке в очередь (domain/events.py), потому что в момент
           отправки воркер уже не знает ни задачи, ни чата'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS markup")
