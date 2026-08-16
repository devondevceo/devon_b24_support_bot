"""tg_bots.miniapp_short_name: короткое имя мини-аппа из BotFather

Мини-апп заводится у КАЖДОГО бота отдельно командой `/newapp`, и его короткое имя
знает только владелец бота, то есть теннант. Общего значения из окружения хватает
ровно на пилот с одним ботом, поэтому оно остаётся как запасное, а настоящее
хранится здесь.

Пусто = мини-апп у теннанта не заведён: кнопки и ссылки на него не показываются
вовсе. Показать ссылку на несуществующее приложение хуже, чем не показать ничего:
Telegram отвечает «приложение не найдено», и это выглядит как поломка продукта.

Revision ID: 0012_miniapp
Revises: 0011_survey_builder
"""
from alembic import op

revision = "0012_miniapp"
down_revision = "0011_survey_builder"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tg_bots ADD COLUMN miniapp_short_name TEXT")
    # Имя приходит из формы и уезжает в URL. Ограничение повторяет правила
    # BotFather: латиница, цифры и подчёркивание, 3–30 символов.
    op.execute("""
        ALTER TABLE tg_bots ADD CONSTRAINT ck_tg_bots_miniapp_short_name
          CHECK (miniapp_short_name IS NULL
                 OR miniapp_short_name ~ '^[A-Za-z][A-Za-z0-9_]{2,29}$')
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE tg_bots DROP CONSTRAINT IF EXISTS ck_tg_bots_miniapp_short_name")
    op.execute("ALTER TABLE tg_bots DROP COLUMN IF EXISTS miniapp_short_name")
