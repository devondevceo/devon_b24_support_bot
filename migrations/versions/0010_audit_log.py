"""audit_log: журнал действий, партиционированный по месяцам

Появляется вместе с назначением админов теннанта — это первое действие из списка
`high_risk` в docs/40-security.md §3, которое совершает человек, а не система.
Вопрос «кто выдал этому человеку права» обязан иметь ответ в тот же день, а не
после разбора логов контейнера, которые живут три ротации по 10 МБ.

Форма — из docs/20-data-model.md §11. Партиция по умолчанию обязательна: без неё
первая же вставка после конца месяца падает, а падать аудит не имеет права.
Ретенция (дроп партиций) и режим `minimal` — отдельной работой, см. 70-plan.md.

Revision ID: 0010_audit_log
Revises: 0009_survey_questions_tenant
"""
from alembic import op

revision = "0010_audit_log"
down_revision = "0009_survey_questions_tenant"
branch_labels = None
depends_on = None

MONTHS = [
    ("2026_08", "2026-08-01", "2026-09-01"),
    ("2026_09", "2026-09-01", "2026-10-01"),
    ("2026_10", "2026-10-01", "2026-11-01"),
    ("2026_11", "2026-11-01", "2026-12-01"),
    ("2026_12", "2026-12-01", "2027-01-01"),
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit_log (
          id          BIGINT      GENERATED ALWAYS AS IDENTITY,
          tenant_id   BIGINT      NOT NULL,
          occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          actor_kind  TEXT        NOT NULL CHECK (actor_kind IN ('user','system','b24')),
          actor_id    BIGINT,
          actor_tg_id BIGINT,
          action      TEXT        NOT NULL,
          client_id   BIGINT,
          project_id  BIGINT,
          target      TEXT,
          detail      JSONB       NOT NULL DEFAULT '{}',
          high_risk   BOOLEAN     NOT NULL DEFAULT false,
          PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
    """)
    for suffix, start, end in MONTHS:
        op.execute(f"CREATE TABLE audit_log_{suffix} PARTITION OF audit_log "
                   f"FOR VALUES FROM ('{start}') TO ('{end}')")
    # Забыли нарезать партиции вперёд — записи уходят сюда, а не теряются.
    op.execute("CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT")

    op.execute("CREATE INDEX ix_audit_log__tenant ON audit_log "
               "(tenant_id, occurred_at DESC)")
    op.execute("CREATE INDEX ix_audit_log__high_risk ON audit_log "
               "(tenant_id, occurred_at DESC) WHERE high_risk")

    op.execute("COMMENT ON COLUMN audit_log.actor_id IS "
               "'ID пользователя Битрикса; для actor_kind=system NULL'")
    op.execute("COMMENT ON TABLE audit_log IS "
               "'Журнал действий. high_risk пишется с полным detail всегда'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log")
