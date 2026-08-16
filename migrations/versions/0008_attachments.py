"""attachments: перенос файлов из Telegram в задачи

Revision ID: 0008_attachments
Revises: 0007_survey
"""
from alembic import op

revision = "0008_attachments"
down_revision = "0007_survey"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tg_attachments (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          idem_key    TEXT        NOT NULL,
          tg_file_id  TEXT        NOT NULL,
          file_name   TEXT,
          size_bytes  BIGINT,
          state       TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','uploaded','failed','rejected')),
          b24_file_id BIGINT,
          error       TEXT,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, idem_key, tg_file_id)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN tg_attachments.state IS
          'uploaded НИКОГДА не перезаливается: ретрай после частичного успеха иначе
           приложит те же файлы второй раз (инвариант И-10)'
    """)
    op.execute("""
        COMMENT ON COLUMN tg_attachments.file_name IS
          'Нормализованное имя: без bidi-символов, без переводов строк, обрезанное.
           U+202E в имени маскирует расширение: «отчет‮gpj.exe» выглядит как jpg'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tg_attachments")
