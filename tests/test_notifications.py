"""Настройки уведомлений: цепочка наследования, сводка и экран настройки.

Цена ошибки здесь необычная: ничего не падает. Разъехавшаяся цепочка не бросает
исключение — она молча шлёт в чат не то, что человек выбрал, и выглядит это как
работающая настройка. Поэтому проверяется не «не упало», а именно результат
разрешения на каждом уровне.

Тесты, которым нужна живая PostgreSQL, помечены `pytestmark` ниже в своём
разделе; без `TEST_DATABASE_URL` пропускаются только они.
"""
from __future__ import annotations

import ast
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from b24bot.api import app_notify
from b24bot.domain import events, notifications
from b24bot.domain.notifications import (
    DEFAULTS,
    EMITTED,
    EVENTS,
    Resolved,
    digest_messages,
    resolve_rows,
    span_text,
)

EVIL = "</span><script>alert(1)</script>\" ' & [b]"


# ------------------------------------------------------------------- реестр
def _codes_in_functions() -> set[str]:
    """Коды событий, которые `events.py` называет внутри кода, а не в таблицах.

    Разбор через `ast`, а не регуляркой по `_change(`: код события уезжает в
    новость и через переменную (`task.completed` против `task.status_changed`
    выбирается тернарником), и регулярка такие места не видит. Таблицы уровня
    модуля — `NOTIFY_ACTIONS`, `NOTIFY_PAYLOAD` — сюда не попадают намеренно:
    строка в таблице кнопок не означает, что уведомление кто-то отправляет.
    """
    tree = ast.parse(Path(events.__file__).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out |= {n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value.startswith("task.")}
    return out


def test_registry_matches_what_the_pipeline_can_send() -> None:
    """Переключатель обязан что-то переключать, а событие — иметь переключатель.

    Обе стороны проверяются по исходнику `events.py`: список кодов там и список
    здесь — это одно и то же обещание, данное в двух местах. Разъехались — в
    интерфейсе появился мёртвый переключатель либо в чат пошло уведомление,
    которое нельзя выключить.
    """
    assert _codes_in_functions() == set(EMITTED), (
        "коды, которые ставит в очередь events.py, разошлись с реестром "
        "notifications.EVENTS")


def test_no_unknown_event_codes_in_the_pipeline() -> None:
    """Опечатка в коде события не падает: она просто никогда не совпадёт с
    настройкой, и уведомление молча не дойдёт."""
    source = Path(events.__file__).read_text(encoding="utf-8")
    for code in set(re.findall(r'"(task\.[a-z_]+)"', source)):
        assert code in DEFAULTS, f"{code} не описан в notifications.EVENTS"


def test_events_have_unique_codes_and_human_text() -> None:
    codes = [e.code for e in EVENTS]
    assert len(codes) == len(set(codes))
    for e in EVENTS:
        assert e.label and e.hint, f"{e.code} без подписи или пояснения"


def test_every_emitted_event_has_a_button_set() -> None:
    """Набор кнопок под уведомлением ищется по коду события через `.get`.

    Пропущенный код не падает, а тихо отдаёт уведомление без кнопок — то есть
    выглядит как решение «кнопок тут не место», принятое кем-то осознанно.
    """
    assert set(EMITTED) <= set(events.NOTIFY_ACTIONS)


def test_defaults_are_the_registry_defaults() -> None:
    assert events.DEFAULTS is DEFAULTS
    assert DEFAULTS["task.created"] is False
    assert DEFAULTS["task.status_changed"] is True


def test_intervals_start_with_immediate_delivery() -> None:
    """Ноль в списке обязателен: это «вернуть как было», а не отсутствие ручки."""
    assert notifications.INTERVALS[0][0] == 0
    assert 0 in notifications.INTERVAL_MINUTES
    assert max(notifications.INTERVAL_MINUTES) <= 1440


# ------------------------------------------------------------- наследование
def _ev(kind: str, code: str, enabled: bool) -> tuple[str, str, bool]:
    return (kind, code, enabled)


def test_absent_row_means_inherit_not_off() -> None:
    """Инвариант И-9. В mclick обратная трактовка стоила инцидента."""
    r = resolve_rows([], [])
    assert r.enabled == DEFAULTS
    assert r.events_from == "default"
    assert r.minutes == 0


def test_explicit_false_beats_default_true() -> None:
    r = resolve_rows([_ev("tenant", "task.status_changed", False)], [])
    assert r.enabled["task.status_changed"] is False
    assert r.events_from == "tenant"


@pytest.mark.parametrize(("rows", "expected"), [
    ([_ev("tenant", "task.completed", False)], False),
    ([_ev("tenant", "task.completed", False),
      _ev("project", "task.completed", True)], True),
    ([_ev("tenant", "task.completed", False),
      _ev("project", "task.completed", True),
      _ev("binding", "task.completed", False)], False),
])
def test_chain_binding_beats_project_beats_tenant(
        rows: list[tuple[str, str, bool]], expected: bool) -> None:
    assert resolve_rows(rows, []).enabled["task.completed"] is expected


def test_level_is_taken_whole_not_merged_code_by_code() -> None:
    """Уровень выигрывает целиком, а не по одному коду.

    Иначе чат, выключивший всё, получал бы событие, которого на его уровне нет,
    из настройки проекта — то есть «выключил всё» означало бы «всё, кроме того,
    что добавят потом».
    """
    rows = [_ev("project", "task.completed", True),
            _ev("project", "task.deadline_changed", True),
            _ev("binding", "task.completed", False)]
    r = resolve_rows(rows, [])
    assert r.enabled["task.completed"] is False
    # На уровне чата про срок ничего не сказано — берётся системный дефолт,
    # а не значение проекта.
    assert r.enabled["task.deadline_changed"] is DEFAULTS["task.deadline_changed"]
    assert r.events_from == "binding"


def test_digest_inherits_independently_of_events() -> None:
    """Две ручки независимы: чат может брать события у проекта и группировать по-своему."""
    r = resolve_rows([_ev("tenant", "task.completed", True)], [("binding", 15)])
    assert r.events_from == "tenant"
    assert (r.minutes, r.minutes_from) == (15, "binding")


def test_digest_zero_is_an_explicit_answer_not_a_missing_row() -> None:
    r = resolve_rows([], [("tenant", 60), ("binding", 0)])
    assert r.minutes == 0
    assert r.minutes_from == "binding"


# -------------------------------------------------------------------- сводка
def test_digest_keeps_every_line_and_says_how_many() -> None:
    lines = [f"строка {i}" for i in range(1, 8)]
    out = digest_messages(lines, timedelta(minutes=15))
    assert len(out) == 1
    assert "7 уведомлений" in out[0]
    for line in lines:
        assert line in out[0]


def test_digest_splits_instead_of_truncating() -> None:
    """Молчаливое усечение здесь запрещено дважды: правилом проекта и смыслом
    сводки — она существует, чтобы новость не пропала."""
    lines = [f"строка {i}" for i in range(1, 61)]
    out = digest_messages(lines, timedelta(minutes=30))
    assert len(out) > 1
    joined = "\n".join(out)
    for line in lines:
        assert line in joined
    assert "из 60" in out[0], "человек обязан видеть, что это часть, и сколько всего"
    for message in out:
        assert len(message) <= 4096, "предел Telegram"


def test_digest_splits_by_length_too_not_only_by_count() -> None:
    lines = ["я" * 400 for _ in range(10)]
    out = digest_messages(lines, timedelta(minutes=5))
    assert len(out) > 1
    for message in out:
        assert len(message) <= 4096


def test_empty_digest_produces_no_message() -> None:
    assert digest_messages([], timedelta(minutes=5)) == []


@pytest.mark.parametrize(("minutes", "expected"), [
    (1, "1 минуту"), (2, "2 минуты"), (5, "5 минут"), (21, "21 минуту"),
    (60, "1 час"), (135, "2 часа 15 минут"), (300, "5 часов"),
])
def test_span_text_is_readable_russian(minutes: int, expected: str) -> None:
    assert span_text(timedelta(minutes=minutes)) == expected


def test_span_is_measured_not_assumed() -> None:
    """Сводка называет фактический возраст самой старой новости.

    «За 15 минут» на сводке, пролежавшей три часа из-за остановленного воркера, —
    уверенная неправда о том, что происходит в проекте.
    """
    out = digest_messages(["строка"], timedelta(hours=3))
    assert "3 часа" in out[0]


# --------------------------------------------------------- краткая форма новости
def test_short_line_is_one_line_and_long_text_keeps_the_stage() -> None:
    ch = events._change("task.status_changed", "🔁", "#233", "Починить кран",
                        "Иван взял в работу", stage_line="\nСтадия: Выполняются")
    assert ch.text.count("\n") == 2
    assert "Стадия: Выполняются" in ch.text
    assert "\n" not in ch.short
    assert "Стадия" not in ch.short, "в строке сводки стадия вытеснила бы саму новость"
    assert "#233" in ch.short and "Починить кран" in ch.short


def test_short_title_is_cut_before_escaping() -> None:
    """Порядок не косметика: `&amp;`, разрезанное посередине, — это мусор на экране."""
    title = "Ромашка & Компания " + "х" * 80
    short = events._short_title(title)
    assert "&amp;" in short
    assert "…" in short
    assert len(short) < len(title)


def test_change_escapes_hostile_title_in_both_forms() -> None:
    ch = events._change("task.created", "🆕", "#1", EVIL, "Создана задача")
    for text in (ch.text, ch.short):
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


# ---------------------------------------------------------------------- экран
def _resolved(**over: object) -> Resolved:
    base = Resolved(enabled=dict(DEFAULTS), minutes=0, events_from="default",
                    minutes_from="default")
    return Resolved(**{**base.__dict__, **over})  # type: ignore[arg-type]


def test_scope_form_escapes_hostile_session_and_carries_tab() -> None:
    html = app_notify._scope_form(EVIL, "notify", "project", 7, None, None,
                                  _resolved(), can_manage=True)
    assert "<script>" not in html
    assert '<input type="hidden" name="tab" value="notify">' in html
    assert 'name="scope_id" value="7"' in html


def test_scope_form_shows_what_applies_now_even_when_inherited() -> None:
    """Пустая форма на унаследованном уровне читалась бы как «всё выключено»."""
    html = app_notify._scope_form("s", "notify", "binding", 3, None, None,
                                  _resolved(enabled={**DEFAULTS,
                                                     "task.created": True}),
                                  can_manage=True)
    checked = re.findall(r'value="(task\.[a-z_]+)" checked', html)
    assert "task.created" in checked
    assert "task.status_changed" in checked


def test_scope_form_mode_reflects_whether_the_level_has_its_own_row() -> None:
    inherited = app_notify._scope_form("s", "notify", "project", 1, None, None,
                                       _resolved(), can_manage=True)
    own = app_notify._scope_form("s", "notify", "project", 1, dict(DEFAULTS), 15,
                                 _resolved(minutes=15, minutes_from="project"),
                                 can_manage=True)
    assert '<option value="inherit" selected>' in inherited
    assert '<option value="custom" selected>' in own
    assert '<option value="15" selected>' in own


def test_tenant_form_has_no_inherit_interval_option() -> None:
    """Наследовать теннанту не у кого: «наследовать» там означало бы «сразу»,
    и два одинаковых по смыслу пункта в одном списке — это ложный выбор."""
    html = app_notify._scope_form("s", "notify", "tenant", 0, None, None,
                                  _resolved(), can_manage=True)
    selects = html.split('name="digest"')[1].split("</select>")[0]
    assert 'value="inherit"' not in selects


def test_form_hidden_for_non_admin() -> None:
    html = app_notify._scope_form("s", "notify", "tenant", 0, None, None,
                                  _resolved(), can_manage=False)
    assert "<form" not in html
    assert "администратор" in html


def test_only_emitted_events_get_a_switch() -> None:
    html = app_notify._scope_form("s", "notify", "tenant", 0, None, None,
                                  _resolved(), can_manage=True)
    assert 'value="task.comment_added"' not in html
    assert 'value="task.status_changed"' in html


def test_summary_counts_only_switchable_events() -> None:
    line = app_notify._summary(_resolved(enabled=dict.fromkeys(DEFAULTS, True)))
    assert f"из {len(EMITTED)}" in line


# ----------------------------------------------------- живая база: хранение
ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

live = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")


async def _world(conn: object) -> dict[str, int]:
    uniq = uuid.uuid4().hex[:8]
    tenant = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        f"t{uniq}", "Теннант", f"member-{uniq}", f"{uniq}.bitrix24.ru")
    client_id = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO clients (tenant_id, name, status) VALUES ($1,$2,'active') "
        "RETURNING id", tenant, "Клиент")
    project = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1,$2,33,'Проект','active') RETURNING id", tenant, client_id)
    # Токен шифруется настоящим `box`: колонка объявлена доменом `enc_text`
    # с проверкой формата, и подсунуть туда «enc» строкой нельзя. Отправка в
    # тестах подменяется, но расшифровка — та же самая, что в бою.
    from b24bot.core.config import get_settings
    from b24bot.crypto import box

    bot_id = int(uuid.uuid4().int % 10**9)
    kid = get_settings().master_key_id
    bot = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO tg_bots (tenant_id, bot_id, username, token, token_kid, "
        "webhook_secret, webhook_secret_kid, status) "
        "VALUES ($1,$2,'bot',$3,$4,$5,$4,'active') RETURNING id",
        tenant, bot_id,
        box.encrypt("123:secret", box.aad("tg_bots", "token", tenant, bot_id)), kid,
        box.encrypt("hook", box.aad("tg_bots", "webhook_secret", tenant, bot_id)))
    chat = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO tg_chats (tenant_id, bot_ref, chat_id, title, status) "
        "VALUES ($1,$2,$3,'Чат','active') RETURNING id",
        tenant, bot, -int(uuid.uuid4().int % 10**9))
    binding = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
        "VALUES ($1,$2,$3,'active') RETURNING id", tenant, chat, project)
    return {"tenant": tenant, "project": project, "bot": bot, "chat": chat,
            "binding": binding}


@live
async def test_saving_a_level_writes_explicit_false_for_every_event(db: object) -> None:
    """И-9: выключенное событие обязано лечь строкой `false`, а не исчезнуть.

    Удалённая строка означает «наследовать», и выключенное на уровне чата
    событие вернулось бы к нему с уровня проекта — человек получил бы ровно то,
    что только что выключил.
    """
    w = await _world(db)
    await notifications.save_events(w["tenant"], "binding", w["binding"],
                                    dict.fromkeys(EMITTED, False))
    rows = await db.fetch(  # type: ignore[attr-defined]
        "SELECT code, enabled FROM notification_settings "
        "WHERE tenant_id = $1 AND scope_kind = 'binding' AND scope_id = $2",
        w["tenant"], w["binding"])
    assert {r["code"] for r in rows} == set(EMITTED)
    assert all(r["enabled"] is False for r in rows)


@live
async def test_returning_a_level_to_inherit_removes_its_rows(db: object) -> None:
    w = await _world(db)
    await notifications.save_events(w["tenant"], "project", w["project"],
                                    dict.fromkeys(EMITTED, True))
    await notifications.save_events(w["tenant"], "project", w["project"], None)
    assert await notifications.scope_events(w["tenant"], "project",
                                            w["project"]) is None


@live
async def test_resolve_reads_the_whole_chain_from_the_database(db: object) -> None:
    w = await _world(db)
    await notifications.save_events(w["tenant"], "tenant", 0,
                                    dict.fromkeys(EMITTED, False))
    await notifications.save_minutes(w["tenant"], "tenant", 0, 60)
    await notifications.save_minutes(w["tenant"], "binding", w["binding"], 5)

    r = await notifications.resolve(w["tenant"], w["project"], w["binding"])
    assert r.enabled["task.status_changed"] is False
    assert (r.minutes, r.minutes_from) == (5, "binding")

    await notifications.save_events(w["tenant"], "binding", w["binding"],
                                    {"task.status_changed": True})
    r = await notifications.resolve(w["tenant"], w["project"], w["binding"])
    assert r.enabled["task.status_changed"] is True


@live
async def test_interval_outside_the_list_is_refused(db: object) -> None:
    """Значение приезжает из браузера портала: окно «на год» тут не заводится."""
    w = await _world(db)
    with pytest.raises(ValueError, match="интервал"):
        await notifications.save_minutes(w["tenant"], "tenant", 0, 7)


@live
async def test_settings_of_two_tenants_do_not_leak(db: object) -> None:
    one, two = await _world(db), await _world(db)
    await notifications.save_minutes(one["tenant"], "tenant", 0, 480)
    r = await notifications.resolve(two["tenant"], two["project"], two["binding"])
    assert r.minutes == 0, "настройка чужого теннанта не имеет права примениться"


# ------------------------------------------------- живая база: очередь и сводка
@live
async def test_disabled_event_never_reaches_the_queue(db: object) -> None:
    w = await _world(db)
    await notifications.save_events(w["tenant"], "binding", w["binding"],
                                    dict.fromkeys(EMITTED, False))
    ch = events._change("task.status_changed", "🔁", "#1", "Задача", "Иван завершил")
    await events._deliver(w["tenant"], w["project"], 1, [ch])
    assert await db.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM outbox WHERE tenant_id = $1", w["tenant"]) == 0


@live
async def test_grouping_holds_the_row_and_second_news_joins_the_same_window(
        db: object) -> None:
    """Окно открывает первая новость, вторая присоединяется к нему.

    Иначе при интервале в 15 минут события, идущие каждые пять, держали бы чат
    в вечном ожидании и сводка не уехала бы никогда.
    """
    w = await _world(db)
    await notifications.save_minutes(w["tenant"], "binding", w["binding"], 15)
    for n in (1, 2):
        await events._deliver(
            w["tenant"], w["project"], n,
            [events._change("task.status_changed", "🔁", f"#{n}", "Задача",
                            "Иван завершил")])

    rows = await db.fetch(  # type: ignore[attr-defined]
        "SELECT digest, digest_text, next_attempt_at, state FROM outbox "
        "WHERE tenant_id = $1 ORDER BY id", w["tenant"])
    assert len(rows) == 2
    assert all(r["digest"] and r["state"] == "pending" for r in rows)
    assert all(r["digest_text"] for r in rows), "краткая форма собирается заранее"
    assert rows[0]["next_attempt_at"] == rows[1]["next_attempt_at"]


@live
async def test_without_grouping_the_row_is_ready_at_once(db: object) -> None:
    w = await _world(db)
    await events._deliver(
        w["tenant"], w["project"], 5,
        [events._change("task.status_changed", "🔁", "#5", "Задача", "Иван завершил")])
    row = await db.fetchrow(  # type: ignore[attr-defined]
        "SELECT digest, digest_text, next_attempt_at <= now() AS ready FROM outbox "
        "WHERE tenant_id = $1", w["tenant"])
    assert row["digest"] is False
    assert row["digest_text"] is None
    assert row["ready"] is True


@live
async def test_flush_sends_one_message_for_many_and_keeps_buttons_for_one(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Сводка из одной новости — это не сводка, а само уведомление.

    Заворачивать её в шапку «сводка · 1 уведомление» значило бы отнять у неё
    кнопки ради формы.
    """
    from b24bot.worker import main as worker

    w = await _world(db)
    sent: list[tuple[str, object]] = []

    async def fake_send(token: str, chat_id: int, text: str, **kw: object) -> None:
        sent.append((text, kw.get("reply_markup")))

    monkeypatch.setattr(worker.tg, "send_message", fake_send)

    async def queue(n: int, digest: bool) -> None:
        await db.execute(  # type: ignore[attr-defined]
            "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, kind, text, "
            "digest_text, digest, markup) "
            "VALUES ($1,$2,$3,'task.status_changed',$4,$5,$6,$7)",
            w["tenant"], w["bot"], w["chat"], f"🔁 <b>#{n}</b> Задача\nИван завершил",
            f"🔁 #{n} Задача — Иван завершил", digest,
            '{"inline_keyboard": [[]]}' if not digest else None)

    await queue(1, True)
    await queue(2, True)
    assert await worker.flush_digests() == 2
    assert len(sent) == 1, "две новости уехали одним сообщением"
    assert "Сводка" in sent[0][0] and "#1" in sent[0][0] and "#2" in sent[0][0]
    assert sent[0][1] is None, "у сводки кнопок нет: они относятся к отдельным задачам"

    sent.clear()
    await queue(3, True)
    assert await worker.flush_digests() == 1
    assert len(sent) == 1
    assert "Сводка" not in sent[0][0], "одна новость уходит обычным уведомлением"

    state = await db.fetch(  # type: ignore[attr-defined]
        "SELECT state FROM outbox WHERE tenant_id = $1", w["tenant"])
    assert all(r["state"] == "sent" for r in state)


@live
async def test_plain_queue_ignores_rows_that_wait_for_their_window(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Иначе накопительная строка уехала бы поодиночке обычной отправкой —
    и группировка не делала бы ничего, оставаясь включённой на вид."""
    from b24bot.worker import main as worker

    w = await _world(db)
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, kind, text, digest, "
        "next_attempt_at) VALUES ($1,$2,$3,'task.status_changed','текст',true,"
        "now() + interval '10 minutes')", w["tenant"], w["bot"], w["chat"])

    async def fail_send(*a: object, **kw: object) -> None:
        raise AssertionError("отправлять ещё нечего")

    monkeypatch.setattr(worker.tg, "send_message", fail_send)
    assert await worker.send_outbox() == 0
    assert await worker.flush_digests() == 0
