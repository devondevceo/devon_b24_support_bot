"""Группа стала супергруппой: чат переезжает на новый chat_id вместе с привязками.

До 23.09.2026 переезда не было: новая супергруппа регистрировалась незаявленным
чатом без привязок, бот отвечал в ней «чат не подключён», уведомления уходили на
мёртвый старый chat_id, а админ перепривязывал проекты руками, не зная почему.

Что здесь дорого и потому проверяется на настоящей PostgreSQL:

* **Привязки переезжают вместе с id.** Настройки уведомлений уровня привязки
  ключуются им: новая строка привязки молча вернула бы чату настройки проекта.
* **Две вести, любой порядок, повторы.** `migrate_to_chat_id` в старой группе и
  `migrate_from_chat_id` в новой приходят обе; итог один — одна живая строка
  на чат, а не две.
* **Новый чат бывает уже заведён** первым апдейтом из супергруппы — так было со
  всеми группами до этой работы. Его строка становится преемником, а не второй
  строкой того же чата.
* **Чужое не трогается.** Переезд идёт в системном скоупе RLS, и страхует тут
  только `tenant_id` в каждом запросе (И-2).
* **Номера сообщений у чатов свои.** Поэтому новому чату — новая строка, а связи
  сообщений остаются у надгробия; страж сверяет с каталогом, что каждая ссылка на
  `tg_chats` названа: переезжает она или остаётся.

Чистые тесты (разбор вестей, ответ Telegram, порядок вызовов в `handle`) живут в
этом же файле выше живых — им база не нужна.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from b24bot.bot import dispatch
from b24bot.domain import audit, chat_migration
from b24bot.tg import api as tg

OLD, NEW = -4_812_345_678, -1_002_345_678_901
USER = {"id": 42, "is_bot": False, "first_name": "Анна"}


def _migrate_to(old: int = OLD, new: int = NEW, title: str = "Поддержка") -> dict[str, Any]:
    """Весть в старой группе: «группа стала супергруппой такой-то»."""
    return {"update_id": 1, "message": {
        "message_id": 310, "date": 0, "from": USER,
        "chat": {"id": old, "type": "group", "title": title},
        "migrate_to_chat_id": new}}


def _migrate_from(old: int = OLD, new: int = NEW, title: str = "Поддержка",
                  is_forum: bool = False) -> dict[str, Any]:
    """Весть в новой супергруппе: «перенесена из такой-то группы»."""
    chat: dict[str, Any] = {"id": new, "type": "supergroup", "title": title}
    if is_forum:
        chat["is_forum"] = True
    return {"update_id": 2, "message": {
        "message_id": 1, "date": 0, "from": USER, "chat": chat,
        "migrate_from_chat_id": old}}


def _plain(chat_id: int, chat_type: str, title: str = "Поддержка") -> dict[str, Any]:
    return {"update_id": 3, "message": {
        "message_id": 2, "date": 0, "from": USER, "text": "добрый день",
        "chat": {"id": chat_id, "type": chat_type, "title": title}}}


# ------------------------------------------------------------ разбор вестей
def test_both_messages_name_the_same_pair() -> None:
    """Каждая весть несёт пару целиком — поэтому любая из двух делает переезд."""
    assert dispatch.migration_of(_migrate_to()) == (OLD, NEW)
    assert dispatch.migration_of(_migrate_from()) == (OLD, NEW)


@pytest.mark.parametrize("update", [
    _plain(OLD, "group"),
    _plain(NEW, "supergroup"),
    # Правка сообщения — не весть: служебные сообщения не правятся, а поле
    # в чужом ключе апдейта значило бы, что мы разбираем не то.
    {"edited_message": _migrate_to()["message"]},
    {"callback_query": {"id": "1", "data": "t:x", "message": _migrate_to()["message"]}},
    {"my_chat_member": {"chat": {"id": NEW, "type": "supergroup"},
                        "new_chat_member": {"status": "member"}}},
    {"update_id": 9},
])
def test_ordinary_updates_are_not_a_migration(update: dict[str, Any]) -> None:
    assert dispatch.migration_of(update) is None


@pytest.mark.parametrize("value", [None, 0, True, "−100500", str(NEW), 1.5, OLD])
def test_malformed_new_chat_id_is_not_a_migration(value: Any) -> None:
    """Строка, дробь, bool (подкласс int!), ноль и «переезд в самого себя»."""
    update = _migrate_to()
    update["message"]["migrate_to_chat_id"] = value
    assert dispatch.migration_of(update) is None


def test_left_mark_is_lifted_when_the_chat_moves() -> None:
    """Весть о переезде пришла боту — значит, он в чате. Правило то же, что у
    `_register_chat`, когда бота возвращают в чат."""
    assert chat_migration.carried_status("left", 7) == "active"
    assert chat_migration.carried_status("left", None) == "unclaimed"
    assert chat_migration.carried_status("active", 7) == "active"
    assert chat_migration.carried_status("unclaimed", None) == "unclaimed"


def test_registries_do_not_overlap() -> None:
    """Таблица либо переезжает, либо остаётся — третьего не дано."""
    assert not set(chat_migration.MOVES) & set(chat_migration.STAYS)


def test_migration_is_a_high_risk_action() -> None:
    """Привязки переезжают без человека — по последствиям это перепривязка."""
    assert "chat.migrate" in audit.HIGH_RISK
    assert {"chat.bind", "chat.unbind"} <= audit.HIGH_RISK


# ------------------------------------------------------------ ответ Telegram
def _telegram_answers(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body)

    monkeypatch.setattr(
        tg, "_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_telegram_names_the_new_chat_in_its_refusal(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Третья весть о переезде — ответ на отправку в старую группу."""
    _telegram_answers(monkeypatch, {
        "ok": False, "error_code": 400,
        "description": "Bad Request: group chat was upgraded to a supergroup chat",
        "parameters": {"migrate_to_chat_id": NEW}})
    with pytest.raises(tg.TelegramError) as caught:
        asyncio.run(tg.send_message("1:x", OLD, "текст"))
    assert caught.value.code == 400
    assert caught.value.migrate_to_chat_id == NEW


@pytest.mark.parametrize("parameters", [None, {}, {"retry_after": 5},
                                        {"migrate_to_chat_id": "−100"},
                                        {"migrate_to_chat_id": True}])
def test_other_refusals_name_no_chat(monkeypatch: pytest.MonkeyPatch,
                                     parameters: dict[str, Any] | None) -> None:
    body: dict[str, Any] = {"ok": False, "error_code": 400,
                            "description": "Bad Request: chat not found"}
    if parameters is not None:
        body["parameters"] = parameters
    _telegram_answers(monkeypatch, body)
    with pytest.raises(tg.TelegramError) as caught:
        asyncio.run(tg.send_message("1:x", OLD, "текст"))
    assert caught.value.migrate_to_chat_id is None


# ------------------------------------------------ порядок вызовов в handle
def _spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def follow(old: int, new: int, **kw: Any) -> chat_migration.Result:
        calls.append(("follow", old, new, kw.get("is_forum"), kw.get("source")))
        return chat_migration.Result(chat_migration.MOVED)

    async def register(chat_id: int, *args: Any) -> None:
        calls.append(("register", chat_id))

    monkeypatch.setattr(dispatch.chat_migration, "follow", follow)
    monkeypatch.setattr(dispatch, "_register_chat", register)
    return calls


def test_message_from_the_old_group_moves_and_registers_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Старая группа упразднена: зарегистрировать её заново — завести призрак."""
    calls = _spy(monkeypatch)
    asyncio.run(dispatch.handle(1, 1, _migrate_to()))
    assert calls == [("follow", OLD, NEW, None, chat_migration.FROM_UPDATE)]


def test_message_from_the_new_group_moves_then_registers_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Сначала переезд, потом регистрация: иначе новый чат завёлся бы ничейным."""
    calls = _spy(monkeypatch)
    asyncio.run(dispatch.handle(1, 1, _migrate_from(is_forum=True)))
    assert calls == [("follow", OLD, NEW, True, chat_migration.FROM_UPDATE),
                     ("register", NEW)]


def test_ordinary_message_only_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    asyncio.run(dispatch.handle(1, 1, _plain(NEW, "supergroup")))
    assert calls == [("register", NEW)]


# ======================================================== живая база
ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

live = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")


def _ids() -> tuple[int, int]:
    """Пара chat_id на тест: база одна на модуль, а chat_id уникален глобально."""
    n = uuid.uuid4().int % 10**9
    return -(n + 1), -(10**12 + n)


async def _world(conn: Any, *, old: int, claimed: bool = True) -> dict[str, int]:
    """Теннант с ботом, клиентом, проектом и старой группой, привязанной к проекту."""
    from b24bot.core.config import get_settings
    from b24bot.crypto import box

    uniq = uuid.uuid4().hex[:8]
    tenant = await conn.fetchval(
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        f"t{uniq}", "Теннант", f"member-{uniq}", f"{uniq}.bitrix24.ru")
    client = await conn.fetchval(
        "INSERT INTO clients (tenant_id, name, status) VALUES ($1,'Линия Жизни','active') "
        "RETURNING id", tenant)
    project = await conn.fetchval(
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1,$2,33,'Поддержка сайта','active') RETURNING id", tenant, client)
    bot_id = int(uuid.uuid4().int % 10**9)
    kid = get_settings().master_key_id
    bot = await conn.fetchval(
        "INSERT INTO tg_bots (tenant_id, bot_id, username, token, token_kid, "
        "webhook_secret, webhook_secret_kid, status) "
        "VALUES ($1,$2,'bot',$3,$4,$5,$4,'active') RETURNING id",
        tenant, bot_id,
        box.encrypt("123:secret", box.aad("tg_bots", "token", tenant, bot_id)), kid,
        box.encrypt("hook", box.aad("tg_bots", "webhook_secret", tenant, bot_id)))
    chat = await conn.fetchval(
        "INSERT INTO tg_chats (chat_id, tenant_id, bot_ref, type, title, status, "
        "first_seen_at, claimed_at) VALUES ($1,$2,$3,'group','Поддержка',$4,"
        "now() - interval '30 days', $5) RETURNING id",
        old, tenant if claimed else None, bot, "active" if claimed else "unclaimed",
        datetime.now(UTC) - timedelta(days=29) if claimed else None)
    world = {"tenant": tenant, "client": client, "project": project, "bot": bot,
             "chat": chat}
    if claimed:
        world["binding"] = await conn.fetchval(
            "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id", tenant, chat, project)
    return world


async def _live_rows(conn: Any, chat_id: int) -> list[Any]:
    return list(await conn.fetch(
        "SELECT * FROM tg_chats WHERE chat_id = $1 AND status <> 'migrated'", chat_id))


async def _audits(conn: Any, tenant: int) -> list[Any]:
    return list(await conn.fetch(
        "SELECT action, target, detail, high_risk FROM audit_log "
        "WHERE tenant_id = $1 AND action = 'chat.migrate'", tenant))


@live
async def test_bindings_and_their_settings_follow_the_chat(db: Any) -> None:
    """Главное обещание: после превращения бот работает с теми же привязками.

    Настройки уведомлений уровня привязки ключуются её id. Заведи переезд новую
    привязку — чат молча вернулся бы к настройкам проекта, ровно к тем, от
    которых его отключали.
    """
    from b24bot.domain import notifications
    from b24bot.domain.context import load_chat_context

    old, new = _ids()
    w = await _world(db, old=old)
    await notifications.save_events(w["tenant"], "binding", w["binding"],
                                    {"task.status_changed": False})
    await notifications.save_minutes(w["tenant"], "binding", w["binding"], 60)
    before = await notifications.resolve(w["tenant"], w["project"], w["binding"])

    await dispatch.handle(w["bot"], w["tenant"], _migrate_to(old, new))

    [chat] = await _live_rows(db, new)
    assert chat["tenant_id"] == w["tenant"]
    assert chat["status"] == "active"
    assert chat["type"] == "supergroup"
    assert chat["title"] == "Поддержка"
    assert chat["id"] != w["chat"], "новому чату — новая строка: номера сообщений свои"
    assert chat["first_seen_at"] < datetime.now(UTC) - timedelta(days=29), \
        "чат тот же: в списке чатов он остаётся на своём месте"

    binding = await db.fetchrow("SELECT id, chat_ref, status FROM chat_bindings "
                                "WHERE id = $1", w["binding"])
    assert (binding["chat_ref"], binding["status"]) == (chat["id"], "active")
    after = await notifications.resolve(w["tenant"], w["project"], w["binding"])
    assert after == before
    assert after.enabled["task.status_changed"] is False
    assert (after.minutes, after.minutes_from) == (60, "binding")

    ctx = await load_chat_context(new)
    assert ctx is not None and ctx.is_active
    assert [p.id for p in ctx.projects] == [w["project"]]
    assert await load_chat_context(old) is None

    tomb = await db.fetchrow("SELECT status, migrated_to, tenant_id FROM tg_chats "
                             "WHERE id = $1", w["chat"])
    assert (tomb["status"], tomb["migrated_to"], tomb["tenant_id"]) == \
        ("migrated", chat["id"], w["tenant"])

    [record] = await _audits(db, w["tenant"])
    detail = json.loads(record["detail"])
    assert record["high_risk"] is True
    assert record["target"] == f"chat:{chat['id']}"
    assert detail["из"] == f"chat:{w['chat']}" and detail["привязок"] == 1
    assert str(old) not in record["detail"] and str(new) not in record["detail"], \
        "chat_id Telegram живёт только в tg_chats.chat_id"


@live
@pytest.mark.parametrize("order", ["to_first", "from_first"])
async def test_either_order_of_the_two_messages_gives_one_chat(
        db: Any, order: str) -> None:
    """Telegram не обещает порядок двух вестей. Итог обязан быть один."""
    old, new = _ids()
    w = await _world(db, old=old)
    first, second = _migrate_to(old, new), _migrate_from(old, new)
    if order == "from_first":
        first, second = second, first

    for update in (first, second):
        await dispatch.handle(w["bot"], w["tenant"], update)

    [chat] = await _live_rows(db, new)
    assert chat["tenant_id"] == w["tenant"] and chat["status"] == "active"
    assert await _live_rows(db, old) == [], "старая группа не воскресла ничейной"
    assert await db.fetchval(
        "SELECT chat_ref FROM chat_bindings WHERE id = $1", w["binding"]) == chat["id"]
    assert len(await _audits(db, w["tenant"])) == 1


@live
async def test_repeated_messages_change_nothing(db: Any) -> None:
    """Поллер при сбое отдаёт апдейт повторно, а в группе бывает два бота."""
    old, new = _ids()
    w = await _world(db, old=old)
    for _ in range(2):
        await dispatch.handle(w["bot"], w["tenant"], _migrate_to(old, new))
        await dispatch.handle(w["bot"], w["tenant"], _migrate_from(old, new))

    assert len(await _live_rows(db, new)) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM tg_chats WHERE chat_id = $1", old) == 1, \
        "у старой группы одно надгробие, а не по одному на повтор"
    assert len(await _audits(db, w["tenant"])) == 1

    again = await chat_migration.follow(old, new, bot_ref=w["bot"],
                                        source=chat_migration.FROM_UPDATE)
    assert again.outcome == chat_migration.ALREADY
    assert not any(again.moved)


@live
async def test_both_messages_at_once_give_one_chat(db: Any) -> None:
    """В группе бывают боты двух теннантов, и каждый разбирает обе вести сам —
    одновременно. Итог тот же: одна живая строка, одна запись в журнале."""
    old, new = _ids()
    w = await _world(db, old=old)
    updates = [_migrate_to(old, new), _migrate_from(old, new)] * 3

    await asyncio.gather(*(dispatch.handle(w["bot"], w["tenant"], u) for u in updates))

    [chat] = await _live_rows(db, new)
    assert chat["tenant_id"] == w["tenant"]
    assert await _live_rows(db, old) == []
    assert await db.fetchval(
        "SELECT chat_ref FROM chat_bindings WHERE id = $1", w["binding"]) == chat["id"]
    assert len(await _audits(db, w["tenant"])) == 1


@live
async def test_new_chat_seen_first_becomes_the_successor(db: Any) -> None:
    """Гонка из жизни: апдейт из новой супергруппы пришёл раньше вести о переезде.

    Так были устроены все превращения до этой работы: новый чат заводился
    незаявленным, и админ видел «чат не подключён». Эта строка и становится
    преемником — второй живой строки на один чат не бывает.
    """
    old, new = _ids()
    w = await _world(db, old=old)
    await dispatch.handle(w["bot"], w["tenant"], _plain(new, "supergroup", "Поддержка!"))
    [phantom] = await _live_rows(db, new)
    assert (phantom["tenant_id"], phantom["status"]) == (None, "unclaimed")

    await dispatch.handle(w["bot"], w["tenant"], _migrate_to(old, new))

    [chat] = await _live_rows(db, new)
    assert chat["id"] == phantom["id"]
    assert (chat["tenant_id"], chat["status"]) == (w["tenant"], "active")
    assert chat["title"] == "Поддержка!", "название — из самой супергруппы"
    assert chat["claimed_at"] is not None
    assert await db.fetchval(
        "SELECT chat_ref FROM chat_bindings WHERE id = $1", w["binding"]) == phantom["id"]
    assert await db.fetchval(
        "SELECT migrated_to FROM tg_chats WHERE id = $1", w["chat"]) == phantom["id"]


@live
async def test_queue_buttons_and_digest_mark_follow_messages_stay(db: Any) -> None:
    """Переезжает относящееся к чату, остаётся относящееся к сообщениям.

    Уведомление в очереди у старой группы упёрлось бы в 400. Его кнопки выданы
    заранее — они тоже переезжают. А связи сообщений и опрос с вопросом в старой
    группе остаются: у новой нумерация своя, и реплай туда уже не дойдёт.
    """
    old, new = _ids()
    w = await _world(db, old=old)
    t, chat = w["tenant"], w["chat"]
    for state in ("pending", "sending", "sent", "failed"):
        await db.execute(
            "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, kind, text, state) "
            "VALUES ($1,$2,$3,'task.status_changed',$4,$5)", t, w["bot"], chat,
            f"уведомление {state}", state)
    for name, ttl in (("живая", timedelta(hours=1)), ("протухшая", timedelta(hours=-1))):
        await db.execute(
            "INSERT INTO callback_tokens (token_hash, tenant_id, kind, chat_ref, "
            "payload, expires_at) VALUES ($1,$2,'notify',$3,'{}',now() + $4::interval)",
            name.encode(), t, chat, ttl)
    template = await db.fetchval("SELECT id FROM survey_templates LIMIT 1")
    await db.execute(
        "INSERT INTO survey_sessions (tenant_id, chat_ref, owner_tg_id, template_id, "
        "last_message_id, expires_at) VALUES ($1,$2,42,$3,305,now() + interval '1 hour')",
        t, chat, template)
    await db.execute(
        "INSERT INTO tg_message_links (tenant_id, chat_ref, message_id, kind, b24_task_id) "
        "VALUES ($1,$2,305,'source',233)", t, chat)
    await db.execute(
        "INSERT INTO reminder_marks (tenant_id, scope, scope_id, kind, fingerprint) "
        "VALUES ($1,'chat',$2,'digest','2026-09-23')", t, chat)

    result = await chat_migration.follow(old, new, bot_ref=w["bot"],
                                         source=chat_migration.FROM_UPDATE)
    assert result.outcome == chat_migration.MOVED
    assert result.moved == chat_migration.Moved(bindings=1, outbox=2, tokens=1, surveys=1)
    successor = result.new_ref

    outbox = {r["state"]: r["chat_ref"] for r in await db.fetch(
        "SELECT state, chat_ref FROM outbox WHERE tenant_id = $1", t)}
    assert outbox == {"pending": successor, "sending": successor,
                      "sent": chat, "failed": chat}
    tokens = {bytes(r["token_hash"]).decode(): r["chat_ref"] for r in await db.fetch(
        "SELECT token_hash, chat_ref FROM callback_tokens WHERE tenant_id = $1", t)}
    assert tokens == {"живая": successor, "протухшая": chat}
    assert await db.fetchval(
        "SELECT state FROM survey_sessions WHERE tenant_id = $1", t) == "cancelled"
    assert await db.fetchval(
        "SELECT chat_ref FROM tg_message_links WHERE tenant_id = $1", t) == chat
    assert await db.fetchval(
        "SELECT scope_id FROM reminder_marks WHERE tenant_id = $1", t) == successor, \
        "отметка утренней сводки переехала: вторая сводка в день переезда — шум"


@live
async def test_other_tenant_is_untouched(db: Any) -> None:
    """Переезд идёт в системном скоупе, RLS здесь не страхует — только И-2."""
    old, new = _ids()
    other_old, _ = _ids()
    mine = await _world(db, old=old)
    other = await _world(db, old=other_old)
    await db.execute(
        "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, kind, text) "
        "VALUES ($1,$2,$3,'task.status_changed','чужое')",
        other["tenant"], other["bot"], other["chat"])
    snapshot = [dict(r) for r in await db.fetch(
        "SELECT 'chat' AS t, id, chat_id AS a, status AS b FROM tg_chats "
        "WHERE tenant_id = $1 UNION ALL "
        "SELECT 'binding', id, chat_ref, status FROM chat_bindings WHERE tenant_id = $1 "
        "UNION ALL SELECT 'outbox', id, chat_ref, state FROM outbox WHERE tenant_id = $1 "
        "ORDER BY 1, 2", other["tenant"])]

    await dispatch.handle(mine["bot"], mine["tenant"], _migrate_to(old, new))

    assert [dict(r) for r in await db.fetch(
        "SELECT 'chat' AS t, id, chat_id AS a, status AS b FROM tg_chats "
        "WHERE tenant_id = $1 UNION ALL "
        "SELECT 'binding', id, chat_ref, status FROM chat_bindings WHERE tenant_id = $1 "
        "UNION ALL SELECT 'outbox', id, chat_ref, state FROM outbox WHERE tenant_id = $1 "
        "ORDER BY 1, 2", other["tenant"])] == snapshot
    assert await _audits(db, other["tenant"]) == []


@live
async def test_new_chat_claimed_by_another_tenant_is_not_taken(db: Any) -> None:
    """Данные одного теннанта не переезжают к другому ни при каком раскладе."""
    old, new = _ids()
    mine = await _world(db, old=old)
    other = await _world(db, old=_ids()[0])
    alien = await db.fetchval(
        "INSERT INTO tg_chats (chat_id, tenant_id, type, title, status) "
        "VALUES ($1,$2,'supergroup','Чужая','active') RETURNING id", new, other["tenant"])

    result = await chat_migration.follow(old, new, bot_ref=mine["bot"],
                                         source=chat_migration.FROM_UPDATE)

    assert result.outcome == chat_migration.CONFLICT
    assert await db.fetchval("SELECT status FROM tg_chats WHERE id = $1",
                             mine["chat"]) == "active"
    assert await db.fetchval("SELECT chat_ref FROM chat_bindings WHERE id = $1",
                             mine["binding"]) == mine["chat"]
    assert await db.fetchval("SELECT tenant_id FROM tg_chats WHERE id = $1",
                             alien) == other["tenant"]
    assert await _audits(db, mine["tenant"]) == []


@live
async def test_new_chat_bound_by_hand_keeps_its_decision(db: Any) -> None:
    """Админ успел привязать новый чат сам. Его решение главнее старого:
    совпавшая привязка не дублируется, недостающая — доезжает."""
    old, new = _ids()
    w = await _world(db, old=old)
    extra = await db.fetchval(
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1,$2,34,'Мобильное приложение','active') RETURNING id",
        w["tenant"], w["client"])
    await db.execute("INSERT INTO chat_bindings (tenant_id, chat_ref, project_id) "
                     "VALUES ($1,$2,$3)", w["tenant"], w["chat"], extra)
    successor = await db.fetchval(
        "INSERT INTO tg_chats (chat_id, tenant_id, type, title, status, claimed_at) "
        "VALUES ($1,$2,'supergroup','Поддержка','active',now()) RETURNING id",
        new, w["tenant"])
    by_hand = await db.fetchval(
        "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id) "
        "VALUES ($1,$2,$3) RETURNING id", w["tenant"], successor, w["project"])

    result = await chat_migration.follow(old, new, bot_ref=w["bot"],
                                         source=chat_migration.FROM_UPDATE)

    assert result.outcome == chat_migration.MOVED and result.new_ref == successor
    live_bindings = await db.fetch(
        "SELECT id, project_id FROM chat_bindings WHERE chat_ref = $1 "
        "AND status = 'active' ORDER BY project_id", successor)
    assert {r["project_id"] for r in live_bindings} == {w["project"], extra}
    assert by_hand in {r["id"] for r in live_bindings}
    assert await db.fetchval(
        "SELECT status FROM chat_bindings WHERE id = $1", w["binding"]) == "disabled", \
        "копия у надгробия не числится живой привязкой несуществующего чата"


@live
async def test_new_chat_bound_to_another_client_is_not_merged(db: Any) -> None:
    """Один чат — один клиент (триггер). Переезд не ломает инвариант, а
    отказывается целиком: наполовину переехавший чат хуже непереехавшего."""
    old, new = _ids()
    w = await _world(db, old=old)
    client = await db.fetchval(
        "INSERT INTO clients (tenant_id, name, status) VALUES ($1,'Другой клиент',"
        "'active') RETURNING id", w["tenant"])
    project = await db.fetchval(
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1,$2,35,'Чужой клиенту проект','active') RETURNING id",
        w["tenant"], client)
    successor = await db.fetchval(
        "INSERT INTO tg_chats (chat_id, tenant_id, type, title, status) "
        "VALUES ($1,$2,'supergroup','Поддержка','active') RETURNING id", new, w["tenant"])
    await db.execute("INSERT INTO chat_bindings (tenant_id, chat_ref, project_id) "
                     "VALUES ($1,$2,$3)", w["tenant"], successor, project)

    result = await chat_migration.follow(old, new, bot_ref=w["bot"],
                                         source=chat_migration.FROM_UPDATE)

    assert result.outcome == chat_migration.CONFLICT
    assert await db.fetchval("SELECT status FROM tg_chats WHERE id = $1",
                             w["chat"]) == "active"
    assert await db.fetchval("SELECT chat_ref FROM chat_bindings WHERE id = $1",
                             w["binding"]) == w["chat"]
    assert await _audits(db, w["tenant"]) == []


@live
async def test_update_from_the_old_group_does_not_resurrect_it(db: Any) -> None:
    """Апдейт из упразднённой группы (нажатие под старым сообщением, запоздалый
    my_chat_member) не заводит её заново ничейной строкой."""
    old, new = _ids()
    w = await _world(db, old=old)
    await dispatch.handle(w["bot"], w["tenant"], _migrate_from(old, new))

    await dispatch.handle(w["bot"], w["tenant"], _plain(old, "group"))
    await dispatch.handle(w["bot"], w["tenant"], {
        "my_chat_member": {"chat": {"id": old, "type": "group", "title": "Поддержка"},
                           "new_chat_member": {"status": "left"}}})

    assert await _live_rows(db, old) == []
    assert await db.fetchval("SELECT status FROM tg_chats WHERE id = $1",
                             w["chat"]) == "migrated"


@live
async def test_unclaimed_group_moves_too(db: Any) -> None:
    """Ничейная группа переезжает так же — иначе она осталась бы призраком, а
    новая завелась бы с чистого листа и потеряла дату первого появления."""
    old, new = _ids()
    w = await _world(db, old=old, claimed=False)
    await dispatch.handle(w["bot"], w["tenant"], _migrate_from(old, new))

    [chat] = await _live_rows(db, new)
    assert (chat["tenant_id"], chat["status"]) == (None, "unclaimed")
    assert await _live_rows(db, old) == []
    assert await db.fetchval("SELECT migrated_to FROM tg_chats WHERE id = $1",
                             w["chat"]) == chat["id"]


@live
async def test_old_miniapp_link_leads_to_the_successor(db: Any) -> None:
    """Ссылка мини-аппа, выданная до превращения, несёт chat_ref надгробия."""
    from b24bot.domain.context import load_chat_context_by_ref

    old, new = _ids()
    w = await _world(db, old=old)
    await dispatch.handle(w["bot"], w["tenant"], _migrate_to(old, new))
    [chat] = await _live_rows(db, new)

    ctx = await load_chat_context_by_ref(w["chat"])
    assert ctx is not None
    assert (ctx.chat_ref, ctx.chat_id, ctx.tenant_id) == (chat["id"], new, w["tenant"])
    assert [p.id for p in ctx.projects] == [w["project"]]


@live
async def test_old_link_never_crosses_to_another_owner(db: Any) -> None:
    """Надгробие ничейной группы, чей преемник потом заявлен теннантом: ссылка
    из ничейного чата к чужому контексту не ведёт."""
    from b24bot.domain.context import load_chat_context_by_ref

    old, new = _ids()
    w = await _world(db, old=old, claimed=False)
    await chat_migration.follow(old, new, bot_ref=w["bot"],
                                source=chat_migration.FROM_UPDATE)
    await db.execute("UPDATE tg_chats SET tenant_id = $2, status = 'active' "
                     "WHERE chat_id = $1 AND status <> 'migrated'", new, w["tenant"])

    assert await load_chat_context_by_ref(w["chat"]) is None


# ---------------------------------------------------- ответ Telegram: воркер
class _Telegram:
    """Bot API в той части, что здесь важна: старая группа отвергает отправку
    и называет новую — ровно как сервер после превращения."""

    def __init__(self, old: int, new: int) -> None:
        self.old, self.new = old, new
        self.delivered: list[tuple[int, str]] = []

    async def send_message(self, token: str, chat_id: int, text: str,
                           **kw: Any) -> dict[str, Any]:
        if chat_id == self.old:
            raise tg.TelegramError(
                400, "Bad Request: group chat was upgraded to a supergroup chat",
                migrate_to_chat_id=self.new)
        self.delivered.append((chat_id, text))
        return {"message_id": 1}


async def _drain(conn: Any) -> None:
    """Воркер работает по всей очереди: строка соседнего теста уехала бы в ту же
    отправку и провалила проверку, ничего не сказав о продукте."""
    await conn.execute("DELETE FROM outbox")


@live
async def test_worker_follows_telegram_answer_and_resends(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Группа стала супергруппой до того, как бот научился переезду: вести давно
    разобраны старым кодом, новый чат заведён ничейным. Единственное, что застаёт
    такой чат, — ответ Telegram на первое же уведомление."""
    from b24bot.worker import main as worker

    old, new = _ids()
    w = await _world(db, old=old)
    await _drain(db)
    await dispatch.handle(w["bot"], w["tenant"], _plain(new, "supergroup"))
    await db.execute(
        "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, kind, text) "
        "VALUES ($1,$2,$3,'task.status_changed','🔁 #233 Задача')",
        w["tenant"], w["bot"], w["chat"])
    telegram = _Telegram(old, new)
    monkeypatch.setattr(worker.tg, "send_message", telegram.send_message)

    await worker.send_outbox()

    row = await db.fetchrow("SELECT state, attempts, chat_ref FROM outbox "
                            "WHERE tenant_id = $1", w["tenant"])
    [chat] = await _live_rows(db, new)
    assert (row["state"], row["attempts"], row["chat_ref"]) == ("pending", 0, chat["id"])
    assert chat["tenant_id"] == w["tenant"]
    [record] = await _audits(db, w["tenant"])
    assert json.loads(record["detail"])["источник"] == chat_migration.FROM_SEND

    await worker.send_outbox()
    assert telegram.delivered == [(new, "🔁 #233 Задача")]
    assert await db.fetchval("SELECT state FROM outbox WHERE tenant_id = $1",
                             w["tenant"]) == "sent"


@live
async def test_worker_sweeps_a_row_queued_to_the_tombstone(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Рассылка прочла старый чат живым за миг до переезда и поставила строку уже
    к надгробию. Повтор переноса её дочищает — иначе она упёрлась бы в 400."""
    from b24bot.worker import main as worker

    old, new = _ids()
    w = await _world(db, old=old)
    await _drain(db)
    await chat_migration.follow(old, new, bot_ref=w["bot"],
                                source=chat_migration.FROM_UPDATE)
    await db.execute(
        "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, kind, text, digest) "
        "VALUES ($1,$2,$3,'task.status_changed','🔁 #234 Задача',true)",
        w["tenant"], w["bot"], w["chat"])
    telegram = _Telegram(old, new)
    monkeypatch.setattr(worker.tg, "send_message", telegram.send_message)

    await worker.flush_digests()
    await worker.flush_digests()

    assert telegram.delivered == [(new, "🔁 #234 Задача")]


@live
async def test_refusal_without_a_successor_fails_as_before(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Новый чат чужой — переезда нет, и строка падает обычным порядком, с
    ответом Telegram в last_error, а не висит в 'sending' навсегда."""
    from b24bot.worker import main as worker

    old, new = _ids()
    w = await _world(db, old=old)
    other = await _world(db, old=_ids()[0])
    await _drain(db)
    await db.execute("INSERT INTO tg_chats (chat_id, tenant_id, type, status) "
                     "VALUES ($1,$2,'supergroup','active')", new, other["tenant"])
    await db.execute(
        "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, kind, text) "
        "VALUES ($1,$2,$3,'task.status_changed','текст')", w["tenant"], w["bot"], w["chat"])
    monkeypatch.setattr(worker.tg, "send_message", _Telegram(old, new).send_message)

    await worker.send_outbox()

    row = await db.fetchrow("SELECT state, last_error FROM outbox WHERE tenant_id = $1",
                            w["tenant"])
    assert row["state"] == "failed" and "upgraded" in row["last_error"]


@live
async def test_probe_finds_a_group_that_moved_while_the_bot_was_deaf(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Вести о переезде разобрал ещё старый код, уведомлений в чате нет — бот
    неделями отвечал бы в новом чате «не подключён». Проверка спрашивает Telegram
    сама, и переезд случается без единого сообщения в чате."""
    old, new = _ids()
    moved_away = await _world(db, old=old)
    quiet, _ = _ids()
    healthy = await _world(db, old=quiet)
    supergroup = _ids()[1]
    await db.execute("INSERT INTO tg_chats (chat_id, tenant_id, bot_ref, type, status) "
                     "VALUES ($1,$2,$3,'supergroup','active')",
                     supergroup, healthy["tenant"], healthy["bot"])
    loose = _ids()[0]
    await db.execute("INSERT INTO tg_chats (chat_id, bot_ref, type, status) "
                     "VALUES ($1,$2,'group','unclaimed')", loose, healthy["bot"])
    asked: list[int] = []

    async def call(token: str, method: str, params: dict[str, Any] | None = None,
                   **kw: Any) -> Any:
        assert method == "getChat" and params is not None
        asked.append(params["chat_id"])
        if params["chat_id"] == old:
            raise tg.TelegramError(
                400, "Bad Request: group chat was upgraded to a supergroup chat",
                migrate_to_chat_id=new)
        return {"id": params["chat_id"], "type": "group"}

    monkeypatch.setattr(chat_migration.tg, "call", call)

    assert await chat_migration.probe_basic_groups() == 1
    assert {old, quiet} <= set(asked)
    assert supergroup not in asked, "супергруппа второй раз не превращается"
    assert loose not in asked, "у ничейной группы переносить нечего"
    [chat] = await _live_rows(db, new)
    assert chat["tenant_id"] == moved_away["tenant"]
    assert await db.fetchval("SELECT chat_ref FROM chat_bindings WHERE id = $1",
                             moved_away["binding"]) == chat["id"]
    assert await db.fetchval("SELECT status FROM tg_chats WHERE id = $1",
                             healthy["chat"]) == "active"
    [record] = await _audits(db, moved_away["tenant"])
    assert json.loads(record["detail"])["источник"] == chat_migration.FROM_PROBE

    asked.clear()
    assert await chat_migration.probe_basic_groups() == 0
    assert old not in asked, "переехавший чат уходит из выборки сам"


# ------------------------------------------------------------- чистка и страж
@live
async def test_purge_goes_through_a_tombstone(db: Any) -> None:
    """Надгробие ничейной группы ссылается на преемника, которого потом заявил
    теннант. Без `ON DELETE SET NULL` (миграция 0021) удаление этого теннанта
    падало бы на ссылке каждые сутки, а обещанный юрдокументами срок молча
    не соблюдался бы."""
    from b24bot.domain import lifecycle

    old, new = _ids()
    w = await _world(db, old=old, claimed=False)
    await chat_migration.follow(old, new, bot_ref=w["bot"],
                                source=chat_migration.FROM_UPDATE)
    await db.execute("UPDATE tg_chats SET tenant_id = $2, status = 'active' "
                     "WHERE chat_id = $1 AND status <> 'migrated'", new, w["tenant"])
    await db.execute("UPDATE tenants SET status = 'uninstalled', "
                     "uninstalled_at = now() - interval '31 days' WHERE id = $1",
                     w["tenant"])

    assert await lifecycle.purge_due() == 1

    tomb = await db.fetchrow("SELECT status, migrated_to FROM tg_chats WHERE id = $1",
                             w["chat"])
    assert (tomb["status"], tomb["migrated_to"]) == ("migrated", None), \
        "надгробие пережило преемника и по-прежнему упраздняет старый chat_id"


@live
async def test_every_reference_to_a_chat_is_accounted_for(db: Any) -> None:
    """Страж: каждая таблица со ссылкой на tg_chats(id) либо переезжает, либо
    остаётся — и это решено в `chat_migration`, а не забыто.

    Список не выписан руками в тесте: он читается из каталога. Новая таблица с
    `chat_ref` провалит этот тест, а не останется молча у надгробия.
    """
    rows = await db.fetch(
        """
        SELECT DISTINCT conrelid::regclass::text AS name FROM pg_constraint
         WHERE contype = 'f' AND confrelid = 'tg_chats'::regclass
           AND conrelid <> 'tg_chats'::regclass
        """)
    found = {r["name"] for r in rows}
    assert found, "каталог пуст — сломан сам страж"
    assert found == set(chat_migration.MOVES) | set(chat_migration.STAYS)
