"""tg_chats: группа стала супергруппой — у старой строки остаётся надгробие

Обычная группа Telegram становится супергруппой сама — при открытии истории новым
участникам, публичной ссылке, включении тем, росте. Чат при этом получает НОВЫЙ
`chat_id`, а переезд делает `domain/chat_migration.py`: новый чат заводит свою строку
`tg_chats`, привязки, очередь и кнопки переезжают к ней, а старая строка остаётся
надгробием — `status='migrated'`, `migrated_to` указывает на преемника.

Колонки и статус заведены ещё миграцией `0002`, но ни разу не использовались, и
две вещи в них к работе не готовы.

1. **`migrated_to` держал удаление преемника.** Внешний ключ без `ON DELETE` —
   это NO ACTION: пока надгробие ничейного чата (`tenant_id IS NULL`) указывает на
   чат, который потом заявил теннант, удаление теннанта падает на этой ссылке.
   Чистка деинсталлированных (`lifecycle.purge_due`) ловила бы исключение каждые
   сутки, а срок удаления данных, обещанный в юрдокументах, молча не соблюдался бы.
   Надгробие без преемника по-прежнему означает «этот chat_id упразднён» —
   поэтому `SET NULL`, а не каскад.
2. **Надгробие ищется на каждом апдейте.** Регистрация чата (`dispatch.handle`)
   не заводит заново упразднённый chat_id — иначе служебное сообщение из старой
   группы воскресило бы её ничейной строкой. Уникальный индекс по `chat_id`
   частичный (`status <> 'migrated'`) и надгробий не видит, поэтому свой индекс.

Комментарий к `tg_chats.id` исправлен: «миграция группы в супергруппу это один
UPDATE» неверно. Номер сообщения Telegram уникален только внутри чата, а
`chat_ref` входит в ключи, где рядом стоит номер сообщения: ключ идемпотентности
`tgsrc-<chat_ref>-<message_id>` живёт тегом на задаче в Битриксе. Сохрани чат
свой `chat_ref` — новое сообщение с тем же номером, что у старого, получило бы
«задача уже создана» и чужую задачу вместо новой (docs/20-data-model.md §4.1).

Revision ID: 0021_chat_migration
Revises: 0020_rls
"""
from alembic import op

revision = "0021_chat_migration"
down_revision = "0020_rls"
branch_labels = None
depends_on = None

_ID_COMMENT_BEFORE = (
    "Суррогатный ключ. ВСЕ прочие таблицы ссылаются на него, а не на телеграмный\n"
    "           chat_id — тогда миграция группы в супергруппу это один UPDATE")
_ID_COMMENT = (
    "Суррогатный ключ. Все прочие таблицы ссылаются на него, а не на телеграмный "
    "chat_id. Группа, ставшая супергруппой, получает НОВУЮ строку: номера сообщений "
    "у чатов свои, а chat_ref входит в ключи рядом с номером сообщения "
    "(tgsrc-<chat_ref>-<message_id>, tg_message_links). Переезд — "
    "domain/chat_migration.py")
_MIGRATED_TO_COMMENT = (
    "Преемник надгробия (status=migrated): строка чата, ставшего супергруппой. "
    "NULL у надгробия — преемника больше нет, но chat_id по-прежнему упразднён")


def upgrade() -> None:
    op.execute("ALTER TABLE tg_chats DROP CONSTRAINT tg_chats_migrated_to_fkey")
    op.execute("ALTER TABLE tg_chats ADD CONSTRAINT tg_chats_migrated_to_fkey "
               "FOREIGN KEY (migrated_to) REFERENCES tg_chats(id) ON DELETE SET NULL")
    op.execute("CREATE INDEX ix_tg_chats__migrated ON tg_chats (chat_id) "
               "WHERE status = 'migrated'")
    op.execute(f"COMMENT ON COLUMN tg_chats.id IS '{_ID_COMMENT}'")
    op.execute(f"COMMENT ON COLUMN tg_chats.migrated_to IS '{_MIGRATED_TO_COMMENT}'")


def downgrade() -> None:
    op.execute("COMMENT ON COLUMN tg_chats.migrated_to IS NULL")
    op.execute(f"COMMENT ON COLUMN tg_chats.id IS '{_ID_COMMENT_BEFORE}'")
    op.execute("DROP INDEX IF EXISTS ix_tg_chats__migrated")
    op.execute("ALTER TABLE tg_chats DROP CONSTRAINT tg_chats_migrated_to_fkey")
    op.execute("ALTER TABLE tg_chats ADD CONSTRAINT tg_chats_migrated_to_fkey "
               "FOREIGN KEY (migrated_to) REFERENCES tg_chats(id)")
