"""survey_questions: собственный tenant_id (инвариант И-2)

Дыра в И-2, найденная тестом-стражем `test_every_domain_table_carries_tenant_id`:
вопрос опросника ссылался только на шаблон, поэтому выборка вопроса по `id` шла
без `tenant_id` и опиралась на дисциплину вызывающего. Колонка nullable, как и у
родителя: NULL означает системный шаблон, общий для всех теннантов.

Revision ID: 0009_survey_questions_tenant
Revises: 0008_attachments
"""
from alembic import op

revision = "0009_survey_questions_tenant"
down_revision = "0008_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE survey_questions ADD COLUMN tenant_id BIGINT "
               "REFERENCES tenants(id) ON DELETE CASCADE")
    op.execute("""
        UPDATE survey_questions q SET tenant_id = t.tenant_id
          FROM survey_templates t WHERE t.id = q.template_id
    """)
    # Расхождение вопроса с шаблоном означало бы, что вопрос одного теннанта
    # приехал в чужой опрос. Проверяем в базе, а не только в коде: CHECK тут
    # невозможен — условие смотрит в соседнюю таблицу.
    op.execute("""
        CREATE OR REPLACE FUNCTION survey_questions_tenant_guard() RETURNS trigger AS $$
        DECLARE owner BIGINT;
        BEGIN
          SELECT tenant_id INTO owner FROM survey_templates WHERE id = NEW.template_id;
          IF owner IS DISTINCT FROM NEW.tenant_id THEN
            RAISE EXCEPTION 'tenant_id вопроса (%) не совпадает с шаблоном (%)',
              NEW.tenant_id, owner;
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_survey_questions_tenant
          BEFORE INSERT OR UPDATE ON survey_questions
          FOR EACH ROW EXECUTE FUNCTION survey_questions_tenant_guard()
    """)
    op.execute("CREATE INDEX ix_survey_questions__tenant "
               "ON survey_questions (tenant_id, template_id, sort)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_survey_questions__tenant")
    op.execute("DROP TRIGGER IF EXISTS trg_survey_questions_tenant ON survey_questions")
    op.execute("DROP FUNCTION IF EXISTS survey_questions_tenant_guard()")
    op.execute("ALTER TABLE survey_questions DROP COLUMN IF EXISTS tenant_id")
