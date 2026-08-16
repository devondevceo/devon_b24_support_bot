"""polling: смещение апдейтов и режим по умолчанию

api.telegram.org недоступен с сервера напрямую, а серверы Telegram не могут достучаться
до нашего домена (проверено 16.08.2026, docs/00-portal-facts.md §13). Приём апдейтов —
long polling через SOCKS5, поэтому боту нужно хранить смещение.

Revision ID: 0003_polling
Revises: 0002_core_domain
"""
from alembic import op

revision = "0003_polling"
down_revision = "0002_core_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tg_bots ADD COLUMN update_offset BIGINT NOT NULL DEFAULT 0")
    op.execute("""
        COMMENT ON COLUMN tg_bots.update_offset IS
          'Последний обработанный update_id. getUpdates вызывается с offset+1:
           так Telegram подтверждает доставку и не присылает апдейт повторно'
    """)
    op.execute("ALTER TABLE tg_bots ALTER COLUMN mode SET DEFAULT 'polling'")
    op.execute("UPDATE tg_bots SET mode = 'polling' WHERE mode = 'webhook'")


def downgrade() -> None:
    op.execute("ALTER TABLE tg_bots ALTER COLUMN mode SET DEFAULT 'webhook'")
    op.execute("ALTER TABLE tg_bots DROP COLUMN IF EXISTS update_offset")
