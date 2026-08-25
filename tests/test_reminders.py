"""Проактивные сообщения: сводка, отметки об отправке, цепочка подтверждения.

Здесь два разных предмета проверки, и они не смешиваются.

**Чистые функции** (сводка и тексты) проверяются везде и без базы. Цена ошибки
в них — неверное число в чате клиента или тег, приехавший из названия проекта.

**Отметки и цепочка напоминаний** проверяются на настоящей PostgreSQL: весь их
смысл в атомарности `INSERT ... ON CONFLICT` и в том, что второе сообщение об
одном и том же не уходит. Подделать это в памяти нельзя, а цена ошибки — поток
одинаковых сообщений человеку либо, наоборот, зависшая навсегда задача.

Запуск второй части: `TEST_DATABASE_URL=postgresql://user:pass@host:5432/postgres pytest`.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from b24bot.domain import reminders

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)          # 23:00 по Москве


def task(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {"id": 1, "title": "Задача", "deadline": None,
                               "changedDate": NOW.isoformat(), "groupId": 33}
    base.update(kw)
    return base


# ------------------------------------------------------------- числа сводки
def test_counts_split_by_deadline() -> None:
    tasks = [
        task(id=1, deadline=(NOW - timedelta(hours=5)).isoformat()),
        task(id=2, deadline=(NOW - timedelta(days=3)).isoformat()),
        task(id=3, deadline=(NOW + timedelta(minutes=30)).isoformat()),
        task(id=4),
    ]
    d = reminders.build_digest(tasks, awaiting=0, now=NOW, tz=MSK)
    assert (d.total, d.overdue, d.today, d.no_deadline) == (4, 2, 1, 1)


def test_today_is_the_portal_local_day_not_the_utc_one() -> None:
    """Срок «завтра в час ночи» по Москве — не сегодня, хотя по UTC ещё сегодня.

    В 23:00 по Москве календарные сутки UTC и местные расходятся, и именно в эти
    часы строка «срок сегодня» читается буквально. Считать по UTC значит обещать
    человеку работу на ночь.
    """
    tomorrow_night = datetime(2026, 8, 26, 1, 0, tzinfo=MSK)
    d = reminders.build_digest([task(deadline=tomorrow_night.isoformat())],
                               awaiting=0, now=NOW, tz=MSK)
    assert (d.today, d.overdue) == (0, 0)

    tonight = datetime(2026, 8, 25, 23, 30, tzinfo=MSK)
    d = reminders.build_digest([task(deadline=tonight.isoformat())],
                               awaiting=0, now=NOW, tz=MSK)
    assert d.today == 1


def test_stale_counts_untouched_tasks() -> None:
    old = (NOW - reminders.STALE_AFTER - timedelta(days=1)).isoformat()
    d = reminders.build_digest([task(changedDate=old), task()], awaiting=0,
                               now=NOW, tz=MSK)
    assert d.stale == 1


def test_silent_when_there_is_nothing_to_report() -> None:
    """Ежедневное «всё в порядке» — шум, который перестают читать вместе со всем."""
    calm = reminders.build_digest([task(), task(id=2)], awaiting=0, now=NOW, tz=MSK)
    assert calm.total == 2 and not calm.worth_sending

    alarming = reminders.build_digest([task()], awaiting=1, now=NOW, tz=MSK)
    assert alarming.worth_sending


# -------------------------------------------------------------- текст сводки
def test_digest_never_shows_task_titles() -> None:
    """Сводка собрана СЕРВИСНЫМ токеном, то есть видит больше читателя чата.

    Числа так показывать можно, заголовки — нет: подробности открываются кнопкой,
    а она уже работает под личным токеном нажавшего, и права режет сам Битрикс.
    """
    tasks = [task(title="Секретное название задачи",
                  deadline=(NOW - timedelta(days=1)).isoformat())]
    d = reminders.build_digest(tasks, awaiting=0, now=NOW, tz=MSK)
    text = reminders.render_digest(d, day=date(2026, 8, 25), projects=["Проект"])
    assert "Секретное" not in text
    assert "Просрочено" in text


def test_digest_escapes_project_names() -> None:
    """И-6: название проекта приходит из Битрикса и подстановкой быть не перестаёт."""
    d = reminders.build_digest([task(deadline=(NOW - timedelta(days=1)).isoformat())],
                               awaiting=0, now=NOW, tz=MSK)
    text = reminders.render_digest(d, day=date(2026, 8, 25),
                                   projects=["<b>Проект</b> & Ко"])
    assert "<b>Проект</b>" not in text
    assert "&lt;b&gt;" in text and "&amp;" in text


def test_digest_says_when_the_portal_gave_only_a_part() -> None:
    """Молчаливое усечение выглядит как баг продукта: числа занижены, а вид у них
    такой же уверенный, как у полных."""
    tasks = [task(id=i, deadline=(NOW - timedelta(days=1)).isoformat())
             for i in range(3)]
    partial = reminders.build_digest(tasks, awaiting=0, now=NOW, tz=MSK, complete=False)
    assert "занижены" in reminders.render_digest(partial, day=date(2026, 8, 25),
                                                 projects=[])
    full = reminders.build_digest(tasks, awaiting=0, now=NOW, tz=MSK)
    assert "занижены" not in reminders.render_digest(full, day=date(2026, 8, 25),
                                                     projects=[])


def test_empty_digest_says_so_plainly() -> None:
    d = reminders.build_digest([], awaiting=0, now=NOW, tz=MSK)
    assert "Открытых задач нет" in reminders.render_digest(
        d, day=date(2026, 8, 25), projects=["Проект"])


# ------------------------------------------------------------ текст эскалации
def test_undelivered_escalation_explains_what_to_do() -> None:
    """Эскалация недоставки — это просьба к чату, а не сообщение об ошибке."""
    text = reminders.escalation_text(ref="#900", title="Задача", who="Иван",
                                     age=timedelta(hours=1), undelivered=True,
                                     has_buttons=True)
    assert "личку" in text
    assert "Кнопки ниже работают" in text

    without = reminders.escalation_text(ref="#900", title="Задача", who="Иван",
                                        age=timedelta(hours=1), undelivered=True,
                                        has_buttons=False)
    assert "не привязан Telegram" in without


def test_escalation_escapes_names_and_titles() -> None:
    text = reminders.escalation_text(ref="#900", title="<i>тест</i>",
                                     who="<b>Иван</b>", age=timedelta(hours=30),
                                     undelivered=False, has_buttons=True)
    assert "<i>тест</i>" not in text and "<b>Иван</b>" not in text


def test_age_reads_like_a_person_would_say_it() -> None:
    assert reminders.fmt_age(timedelta(minutes=10)) == "1 ч"
    assert reminders.fmt_age(timedelta(hours=5)) == "5 ч"
    assert reminders.fmt_age(timedelta(days=3)) == "3 дн"


def test_unknown_timezone_falls_back_to_moscow_not_utc() -> None:
    """UTC сдвинул бы «утро» на три часа и сделал бы «срок сегодня» неправдой."""
    assert reminders.tz_of("Europe/Moscow").utcoffset(NOW) == timedelta(hours=3)
    assert reminders.tz_of("Мордор/Барад-Дур").utcoffset(NOW) == timedelta(hours=3)


# ------------------------------------------------------------------- на базе
async def _world(conn: object) -> dict[str, int]:
    uniq = uuid.uuid4().hex[:8]
    ex = conn.execute        # type: ignore[attr-defined]
    val = conn.fetchval      # type: ignore[attr-defined]

    tenant = await val(
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        f"t{uniq}", "Теннант", f"member-{uniq}", f"{uniq}.bitrix24.ru")
    client_id = await val(
        "INSERT INTO clients (tenant_id, name, status) VALUES ($1,$2,'active') "
        "RETURNING id", tenant, "Клиент")
    project = await val(
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1,$2,33,'Проект','active') RETURNING id", tenant, client_id)

    from b24bot.crypto import box
    # bot_id уникален на всю таблицу: у каждого теннанта свой бот, и два теста
    # с одинаковым номером столкнулись бы в общей базе.
    bot_id = int(uniq, 16) % 10**9
    bot = await val(
        "INSERT INTO tg_bots (tenant_id, bot_id, username, token, token_kid, "
        "webhook_secret, webhook_secret_kid, mode, status) "
        "VALUES ($1,$2,$3,$4,1,$5,1,'polling','active') RETURNING id",
        tenant, bot_id, f"bot{uniq}",
        box.encrypt(f"{bot_id}:секрет", box.aad("tg_bots", "token", tenant, bot_id)),
        box.encrypt("s", box.aad("tg_bots", "webhook_secret", tenant, bot_id)))
    chat = await val(
        "INSERT INTO tg_chats (tenant_id, chat_id, title, type, status, bot_ref) "
        "VALUES ($1,$2,'Чат','group','active',$3) RETURNING id",
        tenant, -int(uuid.uuid4().int % 10**9), bot)
    binding = await val(
        "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
        "VALUES ($1,$2,$3,'active') RETURNING id", tenant, chat, project)

    tg_user_id = int(uuid.uuid4().int % 900_000_000) + 100_000_000
    user = await val(
        "INSERT INTO users (tg_user_id, display_name) VALUES ($1,$2) RETURNING id",
        tg_user_id, "Ответственный")
    await ex("INSERT INTO tenant_members (tenant_id, user_id, role, b24_user_id, "
             "link_status) VALUES ($1,$2,'member',777,'authorized')", tenant, user)
    return {"tenant": tenant, "project": project, "chat": chat, "binding": binding,
            "user": user, "responsible_tg": tg_user_id}


async def _approval(conn: object, w: dict[str, int], *, age: timedelta,
                    notified: bool) -> int:
    value = await conn.fetchval(  # type: ignore[attr-defined]
        """
        INSERT INTO task_approvals (tenant_id, project_id, b24_task_id, task_title,
            responsible_user_id, confirm_stage_id, confirm_stage_title,
            reject_stage_id, reject_stage_title, status, requested_at, notified_at)
        VALUES ($1,$2,900,'Тестовая задача',$3,10,'Да',20,'Нет','pending',
                now() - $4::interval, $5)
        RETURNING id
        """, w["tenant"], w["project"], w["user"], age,
        datetime.now(UTC) - age if notified else None)
    return int(value)


@needs_db
async def test_mark_is_given_out_once_per_reason(db: object) -> None:
    w = await _world(db)
    assert await reminders.mark_once(w["tenant"], "task", 900, "deadline_soon", "A")
    assert not await reminders.mark_once(w["tenant"], "task", 900, "deadline_soon", "A")
    # Перенесли срок — повод новый, и промолчать о нём было бы ошибкой.
    assert await reminders.mark_once(w["tenant"], "task", 900, "deadline_soon", "B")
    assert not await reminders.mark_once(w["tenant"], "task", 900, "deadline_soon", "B")


@needs_db
async def test_digest_off_is_written_explicitly(db: object) -> None:
    """И-9: выключение — это явный `false`, а не удалённая строка.

    Удалением строки настройка вернулась бы к «наследовать выше», и включённый
    у теннанта дайджест продолжил бы приходить в чат, где его только что выключили.
    """
    w = await _world(db)
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO notification_settings (tenant_id, scope_kind, scope_id, code, "
        "enabled) VALUES ($1,'tenant',0,$2,true)", w["tenant"], reminders.CODE_DIGEST)
    assert await reminders.digest_enabled(w["tenant"], w["chat"])

    assert await reminders.set_digest(w["tenant"], w["chat"], False) == 1
    row = await db.fetchrow(  # type: ignore[attr-defined]
        "SELECT enabled FROM notification_settings WHERE tenant_id = $1 "
        "AND scope_kind = 'binding' AND scope_id = $2 AND code = $3",
        w["tenant"], w["binding"], reminders.CODE_DIGEST)
    assert row is not None and row["enabled"] is False
    assert not await reminders.digest_enabled(w["tenant"], w["chat"])


@needs_db
async def test_digest_is_off_until_someone_turns_it_on(db: object) -> None:
    w = await _world(db)
    assert not await reminders.digest_enabled(w["tenant"], w["chat"])
    await reminders.set_digest(w["tenant"], w["chat"], True)
    assert await reminders.digest_enabled(w["tenant"], w["chat"])


@needs_db
async def test_undelivered_request_reaches_the_chat_exactly_once(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Личка недоступна — значит запрос обязан прозвучать в чате, и ровно один раз."""
    from b24bot.domain import dm

    w = await _world(db)
    await _approval(db, w, age=timedelta(hours=1), notified=False)

    async def never_delivered(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(dm, "send", never_delivered)

    assert await reminders.approvals_pass() == 1
    rows = await db.fetch(  # type: ignore[attr-defined]
        "SELECT text, markup FROM outbox WHERE tenant_id = $1 AND kind = $2",
        w["tenant"], "approval.escalated")
    assert len(rows) == 1
    assert "Некому подтвердить" in rows[0]["text"]
    # Кнопки в группе работают и без личного диалога: нажатие его не требует.
    assert "Подтвердить" in str(rows[0]["markup"])

    assert await reminders.approvals_pass() == 0
    again = await db.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM outbox WHERE tenant_id = $1 AND kind = $2",
        w["tenant"], "approval.escalated")
    assert again == 1


@needs_db
async def test_delivered_request_gets_a_reminder_then_an_escalation(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Шаги не складываются: воркер, пролежавший сутки, не шлёт всё пачкой."""
    from b24bot.domain import approvals, dm

    w = await _world(db)
    await _approval(db, w, age=timedelta(days=2), notified=True)
    sent: list[str] = []

    async def remember(tenant_id: int, aid: int, *, heading: str = "") -> bool:
        sent.append(heading)
        return True

    monkeypatch.setattr(approvals, "deliver", remember)
    monkeypatch.setattr(dm, "send", remember)

    assert await reminders.approvals_pass() == 1
    assert sent == [approvals.HEADING_REMINDER], "первым идёт личное напоминание"
    assert await db.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM outbox WHERE tenant_id = $1 AND kind = $2",
        w["tenant"], "approval.escalated") == 0

    assert await reminders.approvals_pass() == 1
    assert await db.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM outbox WHERE tenant_id = $1 AND kind = $2",
        w["tenant"], "approval.escalated") == 1

    # Дальше по цепочке идти некуда: строка выпадает из выборки.
    assert await reminders.approvals_pass() == 0
    assert len(sent) == 1


@needs_db
async def test_reminders_respect_the_setting(db: object,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from b24bot.domain import approvals

    w = await _world(db)
    await _approval(db, w, age=timedelta(days=2), notified=True)
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO notification_settings (tenant_id, scope_kind, scope_id, code, "
        "enabled) VALUES ($1,'project',$2,$3,false)",
        w["tenant"], w["project"], reminders.CODE_APPROVAL)

    async def fail(*args: object, **kwargs: object) -> bool:
        raise AssertionError("выключенное напоминание не имеет права отправиться")

    monkeypatch.setattr(approvals, "deliver", fail)
    assert await reminders.approvals_pass() == 0


@needs_db
async def test_old_marks_are_cleaned_but_only_the_old_ones(db: object) -> None:
    w = await _world(db)
    await reminders.mark_once(w["tenant"], "chat", w["chat"], "digest", "2026-08-25")
    await db.execute(  # type: ignore[attr-defined]
        "UPDATE reminder_marks SET sent_at = now() - interval '1000 days' "
        "WHERE tenant_id = $1 AND scope = 'chat'", w["tenant"])
    await reminders.mark_once(w["tenant"], "task", 900, "deadline_soon", "A")

    assert await reminders.cleanup_marks() >= 1
    left = await db.fetch(  # type: ignore[attr-defined]
        "SELECT scope FROM reminder_marks WHERE tenant_id = $1", w["tenant"])
    assert [r["scope"] for r in left] == ["task"]


@needs_db
async def test_silent_portal_does_not_eat_the_whole_day(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Отметка ставится ДО похода в портал — значит отказ обязан её снимать.

    Иначе один таймаут в девять утра означал бы «сводки сегодня не будет», и
    молча: вопрос «за сегодня» считался бы решённым.
    """
    w = await _world(db)
    await reminders.set_digest(w["tenant"], w["chat"], True)
    # 09:30 по Москве — внутри окна рассылки.
    monkeypatch.setattr(reminders, "_now",
                        lambda: datetime(2026, 8, 25, 6, 30, tzinfo=UTC))

    async def silent(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(reminders, "_open_tasks", silent)
    await reminders.digest_pass()
    assert await _marks(db, w) == 0, "повод остаётся открытым"
    assert await _digests(db, w) == 0

    async def answered(*args: object, **kwargs: object) -> tuple[list[object], bool]:
        return [task(deadline="2026-08-20T18:00:00+03:00")], True

    monkeypatch.setattr(reminders, "_open_tasks", answered)
    await reminders.digest_pass()
    assert await _digests(db, w) == 1
    assert await _marks(db, w) == 1

    # Тот же день — второй сводки не будет.
    await reminders.digest_pass()
    assert await _digests(db, w) == 1


async def _digests(conn: object, w: dict[str, int]) -> int:
    return int(await conn.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM outbox WHERE tenant_id = $1 AND kind = 'digest.daily'",
        w["tenant"]) or 0)


async def _marks(conn: object, w: dict[str, int]) -> int:
    return int(await conn.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM reminder_marks WHERE tenant_id = $1 AND scope = 'chat'",
        w["tenant"]) or 0)
