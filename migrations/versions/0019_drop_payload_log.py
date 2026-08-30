"""Спайковая таблица b24_payload_log удаляется

Заведена миграцией `0001` со сроком жизни «до конца SPIKE A и SPIKE E» — оба
закрыты 16.08.2026. С тех пор таблица жила только потому, что в ней боевые
payload-ы и удаление требовало решения заказчика; решение получено вместе с
подготовкой к Маркету (30.08.2026). Перед выкаткой этой миграции дамп снимается
автоматически (`scripts/deploy.sh`) — данные остаются в дампе.

Вместе с таблицей уходит и её писатель (`api/b24.py::log_payload`) с флагом
`SPIKE_LOG_PAYLOADS`: сырой формат входящих запросов давно зафиксирован в
docs/00-portal-facts.md §10.1, а журнал вызовов теперь ведёт `b24_call_log` —
без тел и с ретенцией.

Downgrade воссоздаёт таблицу пустой: обратная совместимость схемы — да,
воскрешение данных — нет, за этим в дамп.

Revision ID: 0019_drop_payload_log
Revises: 0018_marketplace
"""
from alembic import op

revision = "0019_drop_payload_log"
down_revision = "0018_marketplace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS b24_payload_log")


def downgrade() -> None:
    op.execute("""
        CREATE TABLE b24_payload_log (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          kind        TEXT        NOT NULL,
          member_id   TEXT,
          shape       JSONB       NOT NULL,
          received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_b24_payload_log__kind "
               "ON b24_payload_log (kind, received_at DESC)")
