"""Изоляция теннантов и чатов: инварианты И-2 и И-3 на живой базе.

Единственные тесты, которым нужна настоящая PostgreSQL: проверять SQL по памяти
бессмысленно, ошибка изоляции живёт именно в тексте запроса. Поднимают отдельную
базу, накатывают все миграции, создают двух теннантов с одинаковой формой данных
и убеждаются, что ни один запрос не видит чужого.

Запуск: `TEST_DATABASE_URL=postgresql://user:pass@host:5432/postgres pytest`.
Без переменной модуль пропускается — на ноутбуке без базы сборка не должна краснеть.
Фикстуры базы (`db_url`, `db`) живут в `conftest.py`: ими пользуется не только этот
модуль.

Повод: в `authorize_task_for_chat` добавлена подсказка `group_id_hint` — карточка
задачи стала проверять принадлежность по фактической группе, а не только по кэшу.
Это расширило дверь к задаче, и цена ошибки здесь — чужая переписка в чужом чате.
"""
from __future__ import annotations

import os
import uuid

import pytest

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


async def _fixture_world(conn: object) -> dict[str, int]:
    """Два теннанта одинаковой формы. Второй существует затем, чтобы его было видно,
    если запрос забыл `tenant_id`.

    Уникальный префикс на вызов: база одна на модуль, а `b24_member_id` и `chat_id`
    уникальны глобально — иначе второй тест падал бы на чужих данных, а не на сути.
    """
    uniq = uuid.uuid4().hex[:8]
    base = -abs(hash(uniq)) % 10_000_000 * 100
    ids: dict[str, int] = {}
    for n, tag in ((1, "a"), (2, "b")):
        t = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
            "VALUES ($1, $2, $3, $4, 'active') RETURNING id",
            f"t{uniq}{tag}", f"Теннант {tag}", f"member-{uniq}-{tag}",
            f"t{uniq}{n}.bitrix24.ru")
        c = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO clients (tenant_id, name, status) "
            "VALUES ($1, $2, 'active') RETURNING id", t, f"Клиент {tag}")
        # Одинаковый b24_group_id у обоих теннантов — это законно: порталы разные.
        p = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
            "VALUES ($1, $2, 33, $3, 'active') RETURNING id", t, c, f"Проект {tag}")
        ch = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO tg_chats (tenant_id, chat_id, title, type, status) "
            "VALUES ($1, $2, $3, 'group', 'active') RETURNING id",
            t, -(base + n), f"Чат {tag}")
        await conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
            "VALUES ($1, $2, $3, 'active')", t, ch, p)
        ids |= {f"tenant_{tag}": t, f"client_{tag}": c,
                f"project_{tag}": p, f"chat_{tag}": ch}
    return ids


async def test_hint_does_not_open_another_tenants_task(db: object) -> None:
    """Главный риск новой подсказки: у обоих теннантов группа 33.

    Если бы `project_by_group` искал только по `b24_group_id`, чат теннанта «а»
    открыл бы задачу теннанта «б» — при том, что это вообще другой портал.
    """
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)

    own = await authorize_task_for_chat(w["tenant_a"], w["chat_a"], 500,
                                        group_id_hint=33)
    assert own is not None and own.id == w["project_a"]

    alien = await authorize_task_for_chat(w["tenant_b"], w["chat_a"], 500,
                                          group_id_hint=33)
    assert alien is None, "чат теннанта «а» отдал задачу под теннантом «б»"


async def test_hint_does_not_open_a_task_of_another_chat(db: object) -> None:
    """И-3 в чистом виде: группа существует у теннанта, но не привязана к ЭТОМУ чату."""
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)
    other_project = await db.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1, $2, 77, 'Чужой проект', 'active') RETURNING id",
        w["tenant_a"], w["client_a"])
    other_chat = await db.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO tg_chats (tenant_id, chat_id, title, type, status) "
        "VALUES ($1, $2, 'Чужой чат', 'group', 'active') RETURNING id",
        w["tenant_a"], -(w["chat_a"] * 1_000 + 9001))
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
        "VALUES ($1, $2, $3, 'active')", w["tenant_a"], other_chat, other_project)

    assert await authorize_task_for_chat(w["tenant_a"], w["chat_a"], 600,
                                         group_id_hint=77) is None


async def test_number_scan_finds_nothing(db: object) -> None:
    """Перебор номеров задач из чужого чата обязан молчать на всём диапазоне.

    Кэш наполнен задачами теннанта «б», подсказка не передаётся — ровно то, что
    сделает злоумышленник, подставляя `/t_<id>` в своём чате.
    """
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)
    for task_id in range(100, 120):
        await db.execute(  # type: ignore[attr-defined]
            "INSERT INTO task_cache (tenant_id, b24_task_id, project_id, "
            "b24_group_id, is_ours, title) VALUES ($1,$2,$3,33,true,$4)",
            w["tenant_b"], task_id, w["project_b"], f"Секрет {task_id}")

    for task_id in range(100, 120):
        assert await authorize_task_for_chat(w["tenant_a"], w["chat_a"], task_id) is None


async def test_disabled_binding_closes_the_door(db: object) -> None:
    """Снятая привязка обязана закрывать и подсказку тоже, иначе отзыв доступа фиктивен."""
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)
    await db.execute(  # type: ignore[attr-defined]
        "UPDATE chat_bindings SET status = 'disabled' WHERE chat_ref = $1", w["chat_a"])

    assert await authorize_task_for_chat(w["tenant_a"], w["chat_a"], 500,
                                         group_id_hint=33) is None


async def test_every_domain_table_carries_tenant_id(db: object) -> None:
    """И-2: страж списка таблиц.

    Таблица без `tenant_id` — это таблица, в которой изоляция держится на честном
    слове вызывающего. Новая таблица либо получает колонку, либо явно вносится
    в список исключений вместе с причиной.
    """
    exempt = {
        "alembic_version",   # служебная таблица Alembic
        "tenants",           # сам теннант; его ключ — id
        "enc_keys",          # ключи шифрования общие на инсталляцию
        "users",             # глобальная запись человека: один Telegram-аккаунт
                             # может состоять сразу в нескольких теннантах,
                             # привязка живёт в tenant_members
        "b24_payload_log",   # журнал СТРУКТУРЫ входящих payload: пишется до того,
                             # как теннант вообще определён (событие install).
                             # Таблица спайковая и удаляется отдельной миграцией,
                             # см. docs/80-deploy.md
    }
    rows = await db.fetch(  # type: ignore[attr-defined]
        """
        SELECT t.table_name,
               bool_or(c.column_name = 'tenant_id') AS has_tenant
          FROM information_schema.tables t
          JOIN information_schema.columns c
            ON c.table_schema = t.table_schema AND c.table_name = t.table_name
         WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
         GROUP BY t.table_name
        """)
    missing = sorted(r["table_name"] for r in rows
                     if not r["has_tenant"] and r["table_name"] not in exempt)
    assert not missing, f"таблицы без tenant_id: {missing}"


# --------------------------------------------------------------- роли и аудит
async def _member(conn: object, tenant_id: int, tg_user_id: int, b24_user_id: int,
                  role: str = "member", link: str = "authorized") -> int:
    user_id = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO users (tg_user_id, tg_username, display_name) "
        "VALUES ($1, $2, $3) RETURNING id",
        tg_user_id, f"u{tg_user_id}", f"Человек {tg_user_id}")
    await conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO tenant_members (tenant_id, user_id, role, b24_user_id, "
        "link_status, linked_at) VALUES ($1,$2,$3,$4,$5,now())",
        tenant_id, user_id, role, b24_user_id, link)
    return int(user_id)


async def test_only_tenant_admin_may_bind(db: object) -> None:
    """Матрица прав (docs/40-security.md §3): `/bind` — действие админа теннанта.

    До этого хватало привязанного аккаунта, то есть привязать чат мог и сотрудник
    клиента: он тоже сопоставлен и тоже имеет личный токен.
    """
    from b24bot.domain import access

    w = await _fixture_world(db)
    await _member(db, w["tenant_a"], 1001, 11, role="member")
    await _member(db, w["tenant_a"], 1002, 12, role="tenant_admin")

    assert await access.is_tenant_admin(w["tenant_a"], 1001) is False
    assert await access.is_tenant_admin(w["tenant_a"], 1002) is True
    # Человека вообще нет в теннанте — тоже отказ, а не падение.
    assert await access.is_tenant_admin(w["tenant_a"], 9999) is False


async def test_admin_of_one_tenant_is_not_admin_of_another(db: object) -> None:
    """Роль живёт внутри теннанта. Один телеграм-аккаунт может состоять в двух."""
    from b24bot.domain import access

    w = await _fixture_world(db)
    user_id = await _member(db, w["tenant_a"], 2001, 21, role="tenant_admin")
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO tenant_members (tenant_id, user_id, role, b24_user_id, link_status) "
        "VALUES ($1,$2,'member',$3,'authorized')", w["tenant_b"], user_id, 21)

    assert await access.is_tenant_admin(w["tenant_a"], 2001) is True
    assert await access.is_tenant_admin(w["tenant_b"], 2001) is False


async def test_portal_admin_promotion_is_idempotent(db: object) -> None:
    """Подтягивание админа портала срабатывает один раз, а не пишет аудит на каждый вход."""
    from b24bot.domain import access

    w = await _fixture_world(db)
    await _member(db, w["tenant_a"], 3001, 31, role="member")

    assert await access.promote_portal_admin(w["tenant_a"], 31) is True
    assert await access.promote_portal_admin(w["tenant_a"], 31) is False
    assert await access.role_of_b24_user(w["tenant_a"], 31) == access.TENANT_ADMIN


async def test_last_admin_cannot_be_revoked(db: object) -> None:
    """Иначе теннант остаётся без управления, и вернуть его можно только руками в базе."""
    from b24bot.api.app_ui import _apply_role

    w = await _fixture_world(db)
    only = await _member(db, w["tenant_a"], 4001, 41, role="tenant_admin")
    _, kind = await _apply_role(w["tenant_a"], 41, "revoke", only)
    assert kind == "err"

    second = await _member(db, w["tenant_a"], 4002, 42, role="member")
    _, kind = await _apply_role(w["tenant_a"], 41, "grant", second)
    assert kind == "ok"
    _, kind = await _apply_role(w["tenant_a"], 41, "revoke", only)
    assert kind == "ok"


async def test_unlinked_person_cannot_become_admin(db: object) -> None:
    """Права без личного токена — пустая строка в таблице: действовать в Битриксе нечем."""
    from b24bot.api.app_ui import _apply_role

    w = await _fixture_world(db)
    await _member(db, w["tenant_a"], 5001, 51, role="tenant_admin")
    pending = await _member(db, w["tenant_a"], 5002, None, role="member", link="none")

    _, kind = await _apply_role(w["tenant_a"], 51, "grant", pending)
    assert kind == "err"


async def test_role_change_is_written_to_audit(db: object) -> None:
    """Вопрос «кто выдал этому человеку права» обязан иметь ответ."""
    from b24bot.api.app_ui import _apply_role

    w = await _fixture_world(db)
    await _member(db, w["tenant_a"], 6001, 61, role="tenant_admin")
    target = await _member(db, w["tenant_a"], 6002, 62, role="member")
    await _apply_role(w["tenant_a"], 61, "grant", target)

    row = await db.fetchrow(  # type: ignore[attr-defined]
        "SELECT action, actor_id, target, high_risk, detail FROM audit_log "
        "WHERE tenant_id = $1 ORDER BY occurred_at DESC LIMIT 1", w["tenant_a"])
    assert row["action"] == "role.grant"
    assert row["actor_id"] == 61
    assert row["target"] == f"member:{target}"
    assert row["high_risk"] is True


async def test_audit_of_one_tenant_is_invisible_to_another(db: object) -> None:
    from b24bot.domain import audit

    w = await _fixture_world(db)
    await audit.record(w["tenant_b"], "role.grant", actor_id=7, target="member:7")
    assert await audit.recent(w["tenant_a"]) == []
    assert len(await audit.recent(w["tenant_b"])) == 1


# ------------------------------------------------------- конструктор опросника
async def _template(conn: object, tenant_id: int | None, code: str,
                    questions: int = 2) -> int:
    tid = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO survey_templates (tenant_id, code, title, sort, is_active) "
        "VALUES ($1,$2,$3,10,true) RETURNING id", tenant_id, code, f"Набор {code}")
    for i in range(questions):
        await conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO survey_questions (tenant_id, template_id, sort, code, text) "
            "VALUES ($1,$2,$3,$4,$5)",
            tenant_id, tid, i * 10, f"q{i}", f"Вопрос {i}")
    return int(tid)


async def test_system_template_is_forked_not_edited(db: object) -> None:
    """Системный набор общий на всю инсталляцию.

    Правка одного теннанта меняла бы опросник всем остальным — поэтому первое
    изменение копирует набор себе вместе с вопросами.
    """
    from b24bot.api.app_survey import fork_if_system, questions_of

    w = await _fixture_world(db)
    system = await _template(db, None, f"sys_{uuid.uuid4().hex[:6]}", questions=3)

    mine = await fork_if_system(w["tenant_a"], system)
    assert mine != system

    owner = await db.fetchval(  # type: ignore[attr-defined]
        "SELECT tenant_id FROM survey_templates WHERE id = $1", mine)
    assert owner == w["tenant_a"]
    assert len(await questions_of(w["tenant_a"], mine)) == 3
    # Системный остался нетронутым — им пользуются остальные теннанты.
    assert await db.fetchval(  # type: ignore[attr-defined]
        "SELECT tenant_id FROM survey_templates WHERE id = $1", system) is None


async def test_fork_is_idempotent(db: object) -> None:
    """Второе нажатие «изменить» не должно плодить копии одного набора."""
    from b24bot.api.app_survey import fork_if_system

    w = await _fixture_world(db)
    system = await _template(db, None, f"sys_{uuid.uuid4().hex[:6]}")
    first = await fork_if_system(w["tenant_a"], system)
    assert await fork_if_system(w["tenant_a"], system) == first
    assert await fork_if_system(w["tenant_a"], first) == first


async def test_foreign_template_cannot_be_edited(db: object) -> None:
    """Номер набора в форме подставляется руками — проверка обязана быть на сервере."""
    from b24bot.api.app_survey import fork_if_system

    w = await _fixture_world(db)
    theirs = await _template(db, w["tenant_b"], "chuzhoy")

    with pytest.raises(PermissionError):
        await fork_if_system(w["tenant_a"], theirs)


async def test_questions_of_does_not_leak_across_tenants(db: object) -> None:
    from b24bot.api.app_survey import questions_of

    w = await _fixture_world(db)
    theirs = await _template(db, w["tenant_b"], "chuzhoy", questions=4)

    assert await questions_of(w["tenant_a"], theirs) == []
    assert len(await questions_of(w["tenant_b"], theirs)) == 4


async def test_bot_prefers_own_template_over_the_system_one(db: object) -> None:
    """Ради этого форк и сохраняет `code`: копия сразу перекрывает системный набор."""
    from b24bot.api.app_survey import fork_if_system
    from b24bot.bot.survey import categories

    w = await _fixture_world(db)
    code = f"sys_{uuid.uuid4().hex[:6]}"
    system = await _template(db, None, code)
    mine = await fork_if_system(w["tenant_a"], system)

    ids_a = [i for i, _ in await categories(w["tenant_a"])]
    ids_b = [i for i, _ in await categories(w["tenant_b"])]
    assert mine in ids_a and system not in ids_a
    assert system in ids_b and mine not in ids_b


async def test_question_order_is_repaired_on_move(db: object) -> None:
    """У скопированных вопросов позиции могут совпасть — обмен двух одинаковых
    значений не менял бы ничего, поэтому порядок пересчитывается целиком."""
    from b24bot.api.app_survey import _move, questions_of

    w = await _fixture_world(db)
    tid = await _template(db, w["tenant_a"], "poryadok", questions=3)
    await db.execute(  # type: ignore[attr-defined]
        "UPDATE survey_questions SET sort = 0 WHERE template_id = $1", tid)

    before = [q["id"] for q in await questions_of(w["tenant_a"], tid)]
    await _move(db, w["tenant_a"], tid, before[2], "up")
    after = [q["id"] for q in await questions_of(w["tenant_a"], tid)]
    assert after == [before[0], before[2], before[1]]
