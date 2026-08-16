"""survey: связь вопроса с полем задачи Битрикса и редактируемость наборов

Опросник перестаёт быть зашитым в миграцию: теннант правит наборы в приложении
Битрикса. Отсюда две новые вещи у вопроса — куда класть ответ (`b24_field`) и,
для выпадающего списка, что показывать на кнопках (`options`, колонка была,
но формат не был задан).

`options` — массив `[{"value": "...", "label": "..."}]`. `label` уходит на кнопку
и в тело задачи, `value` — в поле Битрикса. Для `PRIORITY` это «Высокий» → `"2"`:
человек не должен знать про числа, а Битрикс не понимает слов.

Системные наборы (`tenant_id IS NULL`) теннант не правит, а форкает: они общие на
всю инсталляцию, и правка одного теннанта меняла бы опросник всем остальным.
Чтение уже умеет предпочитать свой набор системному с тем же кодом.

Revision ID: 0011_survey_builder
Revises: 0010_audit_log
"""
from alembic import op

revision = "0011_survey_builder"
down_revision = "0010_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `answer_kind` и `options` описаны в docs/20-data-model.md §10 с самого начала,
    # но миграция `0007` их не создала — расхождение вскрылось только сейчас, когда
    # понадобился выпадающий список. Модель данных права, догоняет схема.
    op.execute("""
        ALTER TABLE survey_questions
          ADD COLUMN answer_kind TEXT NOT NULL DEFAULT 'text'
            CHECK (answer_kind IN ('text','choice','file','skip')),
          ADD COLUMN options JSONB
    """)
    op.execute("ALTER TABLE survey_questions ADD COLUMN b24_field TEXT")
    op.execute("ALTER TABLE survey_questions "
               "ADD COLUMN b24_field_type TEXT")
    op.execute("""
        COMMENT ON COLUMN survey_questions.b24_field IS
          'Имя поля задачи в UPPER_SNAKE_CASE; NULL — ответ уходит в тело задачи'
    """)
    op.execute("""
        COMMENT ON COLUMN survey_questions.b24_field_type IS
          'Тип поля с портала на момент привязки: string|enum|integer|datetime|date'
    """)
    op.execute("""
        COMMENT ON COLUMN survey_questions.options IS
          'Для answer_kind=choice: [{"value": "в Битрикс", "label": "на кнопке"}]'
    """)

    # Свой набор перекрывает системный по коду — это уже используется чтением,
    # но ограничения на уровне БД не было, и два своих набора с одним кодом
    # сделали бы выбор недетерминированным.
    op.execute("CREATE UNIQUE INDEX ux_survey_templates__tenant_code "
               "ON survey_templates (tenant_id, code) WHERE tenant_id IS NOT NULL")

    # Порядок вопросов правится кнопками «вверх/вниз»; без уникальности внутри
    # набора два вопроса могли встать на одну позицию и меняться местами при
    # каждом чтении.
    op.execute("CREATE INDEX ix_survey_questions__order "
               "ON survey_questions (template_id, sort)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_survey_questions__order")
    op.execute("DROP INDEX IF EXISTS ux_survey_templates__tenant_code")
    op.execute("ALTER TABLE survey_questions DROP COLUMN IF EXISTS b24_field_type")
    op.execute("ALTER TABLE survey_questions DROP COLUMN IF EXISTS b24_field")
    op.execute("ALTER TABLE survey_questions DROP COLUMN IF EXISTS options")
    op.execute("ALTER TABLE survey_questions DROP COLUMN IF EXISTS answer_kind")
