"""survey: сценарный опросник — шаблоны, вопросы, сессии

Опросник заменяет LLM, которой в фазе 1 нет по решению заказчика. Вопросы лежат
в БД, а не в коде: формулировки для поддержки — это то, что правят по живым
обращениям, и релиз ради запятой никому не нужен.

Revision ID: 0007_survey
Revises: 0006_notifications
"""
from alembic import op

revision = "0007_survey"
down_revision = "0006_notifications"
branch_labels = None
depends_on = None

# Наборы вопросов из docs/30-bot-spec.md §2.2. Порядок важен: первый ответ
# становится заголовком задачи.
TEMPLATES = [
    ("incident", "🔴 Ничего не работает", [
        ("what", "Что именно недоступно? Назовите раздел или адрес страницы.", True),
        ("since", "Когда это началось?", True),
        ("scope", "У всех или только у вас? Если знаете — у скольких человек.", False),
        ("screen", "Что видно на экране: пустая страница, ошибка, вечная загрузка?", False),
        ("blocking", "Работа встала полностью или есть обходной путь?", False),
    ]),
    ("bug", "⚠️ Работает неправильно", [
        ("where", "Где именно: раздел, страница, кнопка?", True),
        ("steps", "Что вы делаете по шагам?", True),
        ("actual", "Что происходит?", True),
        ("expected", "Что вы ожидали увидеть?", False),
        ("since", "Когда заметили впервые?", False),
    ]),
    ("feature", "💡 Нужна доработка", [
        ("what", "Что нужно сделать?", True),
        ("why", "Зачем, какую задачу это решает?", True),
        ("workaround", "Как справляетесь сейчас?", False),
        ("deadline", "Есть ли срок, к которому нужно?", False),
        ("approver", "Кто с вашей стороны принимает результат?", False),
    ]),
    ("question", "❓ Вопрос или консультация", [
        ("question", "Сформулируйте вопрос.", True),
        ("area", "К какому разделу или процессу относится?", False),
        ("urgency", "Насколько срочно нужен ответ?", False),
    ]),
    ("access", "🔑 Нужны доступы", [
        ("who", "Кому нужен доступ: имя и должность.", True),
        ("what", "К чему именно?", True),
        ("level", "Какой уровень: только чтение или изменение?", False),
        ("period", "На какой срок?", False),
        ("approved", "Кто согласовал?", False),
    ]),
    ("other", "📄 Другое", [
        ("free", "Опишите обращение своими словами.", True),
    ]),
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE survey_templates (
          id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
          code      TEXT    NOT NULL,
          title     TEXT    NOT NULL,
          sort      INT     NOT NULL DEFAULT 0,
          is_active BOOLEAN NOT NULL DEFAULT true
        )
    """)
    op.execute("CREATE UNIQUE INDEX ux_survey_templates__code "
               "ON survey_templates (COALESCE(tenant_id, 0), code)")
    op.execute("""
        COMMENT ON COLUMN survey_templates.tenant_id IS
          'NULL = системный набор, доступен всем. Теннант может завести свой с тем же
           кодом — он перекроет системный'
    """)

    op.execute("""
        CREATE TABLE survey_questions (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          template_id BIGINT  NOT NULL REFERENCES survey_templates(id) ON DELETE CASCADE,
          sort        INT     NOT NULL,
          code        TEXT    NOT NULL,
          text        TEXT    NOT NULL,
          required    BOOLEAN NOT NULL DEFAULT false,
          UNIQUE (template_id, code)
        )
    """)

    op.execute("""
        CREATE TABLE survey_sessions (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          chat_ref    BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
          thread_id   BIGINT      NOT NULL DEFAULT 0,
          owner_tg_id BIGINT      NOT NULL,
          template_id BIGINT      NOT NULL REFERENCES survey_templates(id),
          project_id  BIGINT      REFERENCES projects(id) ON DELETE SET NULL,
          step        INT         NOT NULL DEFAULT 0,
          answers     JSONB       NOT NULL DEFAULT '{}',
          last_message_id BIGINT,
          state       TEXT        NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active','done','expired','cancelled')),
          expires_at  TIMESTAMPTZ NOT NULL,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # Сессия ключуется чатом, топиком и автором: в одном чате несколько человек
    # могут вести опросники одновременно и не мешать друг другу.
    op.execute("CREATE UNIQUE INDEX ux_survey_sessions__active "
               "ON survey_sessions (chat_ref, thread_id, owner_tg_id) "
               "WHERE state = 'active'")
    op.execute("""
        COMMENT ON COLUMN survey_sessions.last_message_id IS
          'ID вопроса, отправленного ботом. Ответ принимается ТОЛЬКО реплаем на него:
           приём «любого следующего сообщения» съедал бы обычные реплики коллегам
           и отправлял их в описание задачи'
    """)

    # Значения берутся из константы TEMPLATES в этом же файле; пользовательского
    # ввода здесь нет и быть не может — миграция выполняется один раз при накатке.
    # Одинарные кавычки в текстах всё равно удваиваются: тексты правятся людьми.
    def q(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    for sort, (code, title, questions) in enumerate(TEMPLATES):
        sql = (f"INSERT INTO survey_templates (tenant_id, code, title, sort) "
               f"VALUES (NULL, {q(code)}, {q(title)}, {sort * 10})")
        op.execute(sql)
        for q_sort, (q_code, q_text, required) in enumerate(questions):
            sql = (
                "INSERT INTO survey_questions (template_id, sort, code, text, required) "
                f"SELECT id, {q_sort * 10}, {q(q_code)}, {q(q_text)}, "
                f"{'true' if required else 'false'} FROM survey_templates "
                f"WHERE tenant_id IS NULL AND code = {q(code)}")
            op.execute(sql)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS survey_sessions")
    op.execute("DROP TABLE IF EXISTS survey_questions")
    op.execute("DROP TABLE IF EXISTS survey_templates")
