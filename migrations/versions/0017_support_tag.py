"""tenants.support_tag: тег поддержки на всех задачах, заведённых через бота

Тег нужен, чтобы отделить работу по поддержке от остальной работы в тех же
проектах: по нему отбираются задачи и по нему же режется отчёт о трудозатратах.

**Колонка, а не ключ в `tenants.settings`.** JSONB там лежит с миграции `0001` и
за полтора месяца не получил ни одного читателя и ни одного писателя — то есть
никакой конвенции о ключах не существует, и первый же ключ завёл бы её молча.
Колонка типизирована, видна в `\\d tenants`, а её DEFAULT и есть то самое
«по умолчанию `tg-support`», причём одинаково для уже заведённого теннанта и
для любого будущего.

Пустая строка — осмысленное значение: «не помечать задачи вовсе». Тогда и отчёт
не делится на поддержку и остальное, а показывает одну сумму, как раньше.
NULL для этого не годится: он означал бы «не настроено», а отличать «не настроено»
от «выключено» здесь незачем — и И-9 к теннанту, верхнему уровню цепочки, не
применяется.

`support_tag_synced_at` отмечает разовый проход, который дописал тег задачам,
созданным до появления этой настройки. Без отметки нельзя ни сказать человеку,
что проход уже был, ни объяснить, почему в отчёте до какой-то даты пусто.

Revision ID: 0017_support_tag
Revises: 0016_notification_digest
"""
from alembic import op

revision = "0017_support_tag"
down_revision = "0016_notification_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN support_tag TEXT NOT NULL "
               "DEFAULT 'tg-support'")
    op.execute("ALTER TABLE tenants ADD COLUMN support_tag_synced_at TIMESTAMPTZ")
    op.execute("""
        COMMENT ON COLUMN tenants.support_tag IS
          'Тег, который дописывается КАЖДОЙ задаче, созданной через бота или
           мини-апп, рядом с ключом идемпотентности. По нему отбираются задачи
           поддержки и режется отчёт о трудозатратах. Пустая строка = не
           помечать и не делить отчёт'
    """)
    op.execute("""
        COMMENT ON COLUMN tenants.support_tag_synced_at IS
          'Когда разовый проход дописал тег задачам, созданным до появления
           настройки. NULL = прохода не было, и трудозатраты по старым задачам
           в разрез «поддержка» не попадают'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS support_tag_synced_at")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS support_tag")
