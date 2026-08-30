"""Жизненный цикл тиражного приложения: подписка Маркета, деинсталляция, журнал вызовов

Три группы колонок и одна таблица — всё, чего не хватало схеме, чтобы приложение
могло жить не на одном своём портале, а на любом, который его поставил из
Битрикс24.Маркет.

**Подписка.** Буква статуса приложения (`L` локальное, `F` бесплатное, `D` демо,
`T` триал, `P` платное, `S` подписка Маркет+) приезжает в каждом placement-POST и
в `app.info`. Блокирует функциональность только сочетание «платный статус И
`PAYMENT_EXPIRED=Y`» — правило из mclick (`SubscriptionBlocked`): цена ложного
«не оплачено» для платящего клиента выше цены ложного «оплачено». Правило
вычисляется генерированной колонкой `license_blocked`, чтобы SQL-фильтры воркера
и Python читали ОДНО определение, а не два разъезжающихся. `COALESCE` в выражении
обязателен: `NULL IN (...)` дал бы NULL, и строка выпадала бы из `WHERE NOT
license_blocked` молча.

**Деинсталляция.** `ONAPPUNINSTALL` подтверждается дозапросом нашим токеном (И-8)
и ставит `status='uninstalled'` + `uninstalled_at`. Данные портала живут ещё
`30 дней` (окно на переустановку), после чего воркер удаляет их насовсем —
требование модерации Маркета «удаление данных пользователей при деинсталляции».

**Журнал вызовов.** Требование Маркета к серверным приложениям: «логи
запросов/ответов API за последние 3 суток». Пишется метод, исход и длительность —
без тел запросов (И-1: в телах чужая переписка; И-7: в них же токены).

Revision ID: 0018_marketplace
Revises: 0017_support_tag
"""
from alembic import op

revision = "0018_marketplace"
down_revision = "0017_support_tag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN b24_app_status TEXT")
    op.execute("ALTER TABLE tenants ADD COLUMN license_expired BOOLEAN NOT NULL "
               "DEFAULT false")
    op.execute("""
        ALTER TABLE tenants ADD COLUMN license_blocked BOOLEAN
          GENERATED ALWAYS AS (
            COALESCE(b24_app_status, '') IN ('T','P','S') AND license_expired
          ) STORED
    """)
    op.execute("ALTER TABLE tenants ADD COLUMN b24_app_version BIGINT")
    op.execute("ALTER TABLE tenants ADD COLUMN license_checked_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tenants ADD COLUMN uninstalled_at TIMESTAMPTZ")

    op.execute("""
        COMMENT ON COLUMN tenants.b24_app_status IS
          'Буква статуса приложения из placement/app.info: L локальное, F бесплатное,
           D демо (так смотрит модератор), T триал, P платное, S подписка Маркет+.
           NULL = ещё ни разу не приезжала'
    """)
    op.execute("""
        COMMENT ON COLUMN tenants.license_expired IS
          'PAYMENT_EXPIRED=Y из app.info. Сам по себе ничего не блокирует —
           см. license_blocked'
    """)
    op.execute("""
        COMMENT ON COLUMN tenants.license_blocked IS
          'Функциональность выключена: платный статус (T/P/S) и подписка истекла.
           Единственное место, где это правило определено; код и SQL читают колонку.
           Локальные (L) и бесплатные (F) не блокируются никогда'
    """)
    op.execute("""
        COMMENT ON COLUMN tenants.b24_app_version IS
          'VERSION из app.info. По ней подтверждается подлинность ONAPPUPDATE (И-8):
           событие говорит «приложение обновилось», а версию мы сверяем своим токеном'
    """)
    op.execute("""
        COMMENT ON COLUMN tenants.uninstalled_at IS
          'Когда подтверждён ONAPPUNINSTALL. Через 30 дней данные теннанта удаляются
           насовсем (lifecycle.purge_due); переустановка до этого срока всё сохраняет'
    """)

    # Журнал вызовов REST. Без тел: требование Маркета — «логи запросов/ответов»,
    # инварианты И-1/И-7 — «без переписки и без секретов». Компромисс: метод,
    # исход, длительность. Ретенция 3 суток, чистится воркером.
    op.execute("""
        CREATE TABLE b24_call_log (
          id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          method      TEXT        NOT NULL,
          ok          BOOLEAN     NOT NULL,
          error_code  TEXT,
          duration_ms INT         NOT NULL,
          at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_b24_call_log__at ON b24_call_log (at)")
    op.execute("CREATE INDEX ix_b24_call_log__tenant "
               "ON b24_call_log (tenant_id, at DESC)")
    op.execute("""
        COMMENT ON TABLE b24_call_log IS
          'Требование Битрикс24.Маркет к серверным приложениям: журнал вызовов API
           за последние 3 суток. Тел запросов нет намеренно (И-1, И-7)'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS b24_call_log")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS uninstalled_at")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS license_checked_at")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS b24_app_version")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS license_blocked")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS license_expired")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS b24_app_status")
