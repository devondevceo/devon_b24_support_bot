"""tg_bots.heard_at и tg_bots.poll_error: слышит ли поллер Telegram

Две колонки ради одного вопроса, на который до сих пор нельзя было ответить
ниоткуда, кроме `docker logs` на сервере: «бот забирает сообщения или нет».

23.09.2026 бот перестал отвечать на команды, а всё, что умеет краснеть, было
зелёным: контейнер `healthy`, `/health` отвечает `ok`, приложение в Битриксе пишет
«Интеграция работает». Пульс поллера засчитывал оборот цикла, а оборотом был и
409 от Telegram, и отказ прокси, и таймаут — то есть цикл, в котором ни один
`getUpdates` не прошёл, числился живым. 08.09.2026 так же молча прошли
одиннадцать дней.

* `heard_at` — когда Telegram в последний раз ОТВЕТИЛ на `getUpdates` (пустой
  ответ — тоже ответ). Пишет поллер, не чаще раза в минуту.
* `poll_error` — почему ответов нет, человеческим языком. NULL, пока поллер
  слышит Telegram; заполняется, когда глухота длится дольше `DEAF_AFTER`.

Почему не `last_check_at` и `last_error`. Они уже заняты другим смыслом:
`last_error` объясняет, почему бот ВЫКЛЮЧЕН (`status` = `error`/`suspended`), а
`last_check_at` пишут и проверка из приложения, и разбор пачки апдейтов. Причина
глухоты при `status='active'` в той же колонке значила бы одну строку на два
смысла — и экран не смог бы отличить «бот выключен» от «бот включён, но не слышит».

Downgrade просто убирает колонки: данные в них — оперативное состояние, а не
история, и восстанавливать их нечего.

Revision ID: 0021_bot_heard
Revises: 0020_rls
"""
from alembic import op

revision = "0021_bot_heard"
down_revision = "0020_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tg_bots ADD COLUMN heard_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tg_bots ADD COLUMN poll_error TEXT")
    op.execute("""
        COMMENT ON COLUMN tg_bots.heard_at IS
          'Когда Telegram в последний раз ответил на getUpdates. Пишет поллер, '
          'не чаще раза в минуту. NULL — ещё не отвечал с момента миграции 0021.'
    """)
    op.execute("""
        COMMENT ON COLUMN tg_bots.poll_error IS
          'Почему поллер не получает апдейты, если он глух дольше DEAF_AFTER. '
          'NULL — слышит. Не путать с last_error: тот объясняет status error/suspended.'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE tg_bots DROP COLUMN IF EXISTS poll_error")
    op.execute("ALTER TABLE tg_bots DROP COLUMN IF EXISTS heard_at")
