"""task_approval_settings, task_approvals: подтверждение задачи ответственным

Опциональная фича на уровне проекта: включена — новая задача не начинает жить
молча, а ждёт решения назначенного человека, и решение двигает её на одну из
двух заранее выбранных стадий канбана. Стадии проектные (у каждого проекта свой
канбан), поэтому настройка целиком на уровне project_id, а не наследуется по
tenant/binding, как notification_settings: стадию тенанта наследовать нечем.

task_approvals хранит СНИМОК стадий и ответственного на момент запроса, а не
ссылку на текущие task_approval_settings: правка настроек после отправки запроса
не должна задним числом менять уже отправленный человеку выбор.

Revision ID: 0013_task_approval
Revises: 0012_miniapp
"""
from alembic import op

revision = "0013_task_approval"
down_revision = "0012_miniapp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE task_approval_settings (
          tenant_id            BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          project_id           BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          enabled               BOOLEAN     NOT NULL DEFAULT false,
          responsible_user_id  BIGINT      REFERENCES users(id),
          confirm_stage_id     BIGINT,
          confirm_stage_title  TEXT        NOT NULL DEFAULT '',
          reject_stage_id      BIGINT,
          reject_stage_title   TEXT        NOT NULL DEFAULT '',
          updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, project_id)
        )
    """)
    op.execute("COMMENT ON TABLE task_approval_settings IS "
               "'Опция на уровне проекта: подтверждение новой задачи ответственным. "
               "enabled=true требует заполненных responsible_user_id и обеих стадий "
               "— это проверяется в коде, не констрейнтом (см. api/app_approval.py)'")

    op.execute("""
        CREATE TABLE task_approvals (
          id                   BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id            BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          project_id           BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          b24_task_id          BIGINT      NOT NULL,
          task_title           TEXT        NOT NULL DEFAULT '',
          responsible_user_id  BIGINT      NOT NULL REFERENCES users(id),
          confirm_stage_id     BIGINT      NOT NULL,
          confirm_stage_title  TEXT        NOT NULL DEFAULT '',
          reject_stage_id      BIGINT      NOT NULL,
          reject_stage_title   TEXT        NOT NULL DEFAULT '',
          status               TEXT        NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending','confirmed','rejected')),
          requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          resolved_at          TIMESTAMPTZ
        )
    """)
    op.execute("COMMENT ON TABLE task_approvals IS "
               "'Один запрос на подтверждение задачи. Снимок стадий и ответственного "
               "на момент запроса — правка настроек проекта не меняет уже отправленные'")

    # Двойной hook (ретрай на уровне вызывающего) не должен породить второй
    # параллельный запрос на ту же задачу — идемпотентность на уровне таблицы,
    # а не только на уровне вызывающего кода (И-10).
    op.execute("""
        CREATE UNIQUE INDEX ux_task_approvals__task_pending
          ON task_approvals (tenant_id, b24_task_id) WHERE status = 'pending'
    """)
    # Список «Ожидают подтверждения» — ровно этот фильтр, и он на пути пользователя.
    op.execute("""
        CREATE INDEX ix_task_approvals__responsible_pending
          ON task_approvals (tenant_id, responsible_user_id) WHERE status = 'pending'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_approvals")
    op.execute("DROP TABLE IF EXISTS task_approval_settings")
