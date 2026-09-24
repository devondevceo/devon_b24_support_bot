"""tg_bots: отчёт поллера о приёме — слышит ли бот Telegram

Четыре колонки ради одного вопроса, на который до сих пор нельзя было ответить
ниоткуда, кроме `docker logs` на сервере: «бот забирает сообщения или нет».

23.09.2026 бот восемь часов не забирал ни одного апдейта, а всё, что умеет
краснеть, было зелёным: контейнер `healthy`, `/health` отвечает `ok`, приложение в
Битриксе пишет «Интеграция работает». Пульс засчитывал оборот цикла, а оборотом
был и оборванный заход. 08.09.2026 так же молча прошли одиннадцать дней.

Все четыре пишет поллер одной строкой раз в минуту (`bot/poller.py`,
`_check_reception`), читает экран приложения (`bot/reception.on_screen`):

* `heard_at` — когда Telegram в последний раз ОТВЕТИЛ на `getUpdates` (пустой
  ответ — тоже ответ). NULL — не отвечал с тех пор, как бота подключили.
* `poll_error` — что не так с приёмом, человеческим языком: глухота, стоящая
  очередь, вторая копия бота. NULL — всё в порядке.
* `queue_pending` — `pending_update_count` из `getWebhookInfo`: сколько апдейтов
  Telegram держит для бота. Единственное число, которое отличает «в чатах тихо» от
  «мы не забираем сообщения». NULL — не удалось узнать.
* `poll_checked_at` — когда поллер отчитался. Если служба бота не работает, писать
  причину некому, и тогда говорит возраст этой отметки. Вкладка «Бот» ставит её при
  подключении — с этого момента служба обязана отозваться.

Почему не `last_check_at` и `last_error`. Они уже заняты другим смыслом:
`last_error` объясняет, почему бот ВЫКЛЮЧЕН (`status` = `error`/`suspended`), а
`last_check_at` пишут и проверка из приложения, и разбор пачки апдейтов — в тихом
чате он стоит и у исправного бота. Причина глухоты при `status='active'` в той же
колонке значила бы одну строку на два смысла, и экран не отличил бы «бот выключен»
от «бот включён, но не слышит».

Существующие строки получают NULL во всех четырёх: «служба ещё не отчиталась», а
не сбой. Служба бота перезапускается выкаткой следом и отчитывается за минуту.
Downgrade просто убирает колонки: это оперативное состояние, а не история.

Revision ID: 0022_bot_reception
Revises: 0021_chat_migration
"""
from alembic import op

revision = "0022_bot_reception"
down_revision = "0021_chat_migration"
branch_labels = None
depends_on = None

_COMMENTS = {
    "heard_at": "Когда Telegram в последний раз ответил на getUpdates. Пишет поллер "
                "раз в минуту. NULL — не отвечал с подключения бота.",
    "poll_error": "Что не так с приёмом: глухота, стоящая очередь, вторая копия бота. "
                  "NULL — в порядке. Не путать с last_error: тот объясняет status "
                  "error/suspended.",
    "queue_pending": "pending_update_count из getWebhookInfo на момент poll_checked_at. "
                     "NULL — не удалось узнать.",
    "poll_checked_at": "Когда поллер отчитался о приёме (раз в минуту) или когда бота "
                       "подключили. Старая отметка — служба бота не работает.",
}


def upgrade() -> None:
    op.execute("ALTER TABLE tg_bots ADD COLUMN heard_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tg_bots ADD COLUMN poll_error TEXT")
    op.execute("ALTER TABLE tg_bots ADD COLUMN queue_pending INTEGER "
               "CHECK (queue_pending >= 0)")
    op.execute("ALTER TABLE tg_bots ADD COLUMN poll_checked_at TIMESTAMPTZ")
    for column, text in _COMMENTS.items():
        literal = text.replace("'", "''")
        op.execute(f"COMMENT ON COLUMN tg_bots.{column} IS '{literal}'")


def downgrade() -> None:
    for column in reversed(list(_COMMENTS)):
        op.execute(f"ALTER TABLE tg_bots DROP COLUMN IF EXISTS {column}")
