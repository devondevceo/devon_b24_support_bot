"""RLS: второй рубеж изоляции теннантов (docs/20-data-model.md §13)

Политика Row-Level Security на каждой таблице с `tenant_id` и на самой `tenants`.
Первый рубеж — обязательный tenant_id в каждом запросе (И-2, тест-страж); этот —
страховка в самой базе: запрос с забытым фильтром или пролезшая инъекция не
увидят чужих строк даже теоретически.

Четыре решения, без которых миграция была бы опасной:

1. **Enforce объявляет само соединение** (`app.rls='enforce'`, ставит фасад пула
   при `RLS_ENFORCE=true`). Соединение без объявления — старый образ при новой
   схеме, psql руками, alembic — работает как раньше. Это вперёд-совместимость
   буквально: политики можно накатить, не трогая работающий прод, и включить
   отдельным шагом (docs/80-deploy.md §9). Инъекция через asyncpg объявление не
   снимет: extended-протокол не пускает вторую команду в один запрос.
2. **FORCE ROW LEVEL SECURITY.** Приложение подключается владельцем базы, а
   владельца обычный ENABLE не ограничивает. Отдельная роль без BYPASSRLS (как
   в первоначальном плане §13) потребовала бы второй пары кредов и ручной
   операции на сервере; FORCE подчиняет политике и владельца — тот же эффект
   без новых секретов. Отступление от плана записано в §13.
3. **`tenant_id IS NULL` — общие строки, и они видимы всем.** Незаявленные чаты
   (`tg_chats.tenant_id IS NULL` до привязки) и системные наборы опросника
   (`survey_templates.tenant_id IS NULL`) кросс-теннантны по построению; политика
   без этой ветки молча сломала бы привязку чата и системные опросники.
4. **Список таблиц не перечислен руками.** Цикл по information_schema накрывает
   все нынешние таблицы с tenant_id, включая партиции audit_log (у них своя
   строка в каталоге, прямое обращение к партиции тоже под политикой). Новая
   таблица политику отсюда НЕ унаследует — её обязана дать её собственная
   миграция, а страж `tests/test_rls.py` сверяет каталог: таблица с tenant_id
   без rowsecurity валит прогон.

Контекст (`app.ctx`) ставит фасад пула из contextvars: '<tenant_id>' — строки
теннанта, 'system' — все (инфраструктурные выборки воркера и резолв теннанта по
недоверенному идентификатору), пусто — не видно ничего (fail-closed: путь без
контекста ломается громко, а не читает чужое).

Revision ID: 0020_rls
Revises: 0019_drop_payload_log
"""
from alembic import op

revision = "0020_rls"
down_revision = "0019_drop_payload_log"
branch_labels = None
depends_on = None

# Выражение политики. current_setting(..., true) отдаёт NULL на незаданной
# переменной: NULL IS DISTINCT FROM 'enforce' истинно — эскейп работает и для
# соединений, которые про RLS не знают вовсе.
_GUARD = ("current_setting('app.rls', true) IS DISTINCT FROM 'enforce' "
          "OR current_setting('app.ctx', true) = 'system'")
_TENANT_ROWS = ("tenant_id IS NULL "
                "OR tenant_id::text = current_setting('app.ctx', true)")
_TENANTS_SELF = "id::text = current_setting('app.ctx', true)"


def upgrade() -> None:
    # Условие политики содержит одинарные кавычки, поэтому внутри DO-блока оно
    # передаётся долларовой строкой ($c$...$c$), а не обычным литералом.
    op.execute(f"""
        DO $do$
        DECLARE
            t RECORD;
            cond TEXT := $c${_GUARD} OR {_TENANT_ROWS}$c$;
        BEGIN
            FOR t IN
                SELECT c.table_name
                  FROM information_schema.columns c
                  JOIN information_schema.tables tb
                    ON tb.table_schema = c.table_schema
                   AND tb.table_name = c.table_name
                 WHERE c.table_schema = 'public'
                   AND c.column_name = 'tenant_id'
                   AND tb.table_type = 'BASE TABLE'
            LOOP
                EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',
                               t.table_name);
                EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',
                               t.table_name);
                EXECUTE format(
                    'CREATE POLICY p_tenant_isolation ON %I FOR ALL '
                    'USING (%s) WITH CHECK (%s)',
                    t.table_name, cond, cond);
            END LOOP;
        END
        $do$
    """)
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY p_tenant_isolation ON tenants FOR ALL
        USING ({_GUARD} OR {_TENANTS_SELF})
        WITH CHECK ({_GUARD} OR {_TENANTS_SELF})
    """)

    # Роль приложения — как и требовал план §13. Сначала казалось, что FORCE
    # достаточно и без неё, но проверка на живом сервере показала `usesuper=true`:
    # bootstrap-пользователь контейнера postgres — суперпользователь КЛАСТЕРА,
    # а суперпользователя row security не касается вообще, FORCE или нет.
    # Роль создаётся NOLOGIN и без пароля (секретам в миграциях не место):
    # вход включает оператор при переключении DATABASE_URL (docs/80-deploy.md
    # §9), а в тестах — фикстура conftest на своём одноразовом кластере. До
    # этого роль просто существует и никому не мешает. Роль кластерная, поэтому
    # CREATE обёрнут в exception-блок: вторая база того же кластера находит её
    # уже созданной.
    op.execute("""
        DO $do$
        BEGIN
            CREATE ROLE b24bot_app NOLOGIN NOSUPERUSER NOBYPASSRLS
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END
        $do$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO b24bot_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
               "IN SCHEMA public TO b24bot_app")
    op.execute("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES "
               "IN SCHEMA public TO b24bot_app")
    # Будущие таблицы (их создаёт владелец при миграциях) получают права сами —
    # иначе каждая новая миграция была бы обязана помнить про GRANT.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO b24bot_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO b24bot_app")


def downgrade() -> None:
    # Права роли снимаются, сама роль остаётся: она кластерная, и в соседней базе
    # того же кластера (тесты, будущие стенды) на неё могут держаться гранты.
    # Бесправная login-роль без пароля — безвредный артефакт.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM b24bot_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               "REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM b24bot_app")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM b24bot_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM b24bot_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM b24bot_app")
    # pg_class, а не pg_tables: последняя не показывает партиционированного
    # родителя (relkind 'p'), и audit_log остался бы с политикой навсегда —
    # повторный upgrade падал бы на CREATE POLICY.
    op.execute("""
        DO $do$
        DECLARE
            t RECORD;
        BEGIN
            FOR t IN
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relkind IN ('r', 'p')
                   AND c.relrowsecurity
            LOOP
                EXECUTE format(
                    'DROP POLICY IF EXISTS p_tenant_isolation ON %I',
                    t.relname);
                EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',
                               t.relname);
                EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY',
                               t.relname);
            END LOOP;
        END
        $do$
    """)
