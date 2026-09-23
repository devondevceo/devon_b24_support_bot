"""Группа Telegram стала супергруппой: чат переезжает на новый `chat_id`.

Обычная группа превращается в супергруппу сама — при открытии истории новым
участникам, публичной ссылке, включении тем, росте. Telegram при этом выдаёт чату
НОВЫЙ `chat_id` и сообщает об этом трижды:

* служебным сообщением `migrate_to_chat_id` в старой группе;
* служебным сообщением `migrate_from_chat_id` в новой супергруппе;
* ответом на отправку в старую группу: 400 с `parameters.migrate_to_chat_id`.

Первые два приходят оба, в любом порядке, и каждое несёт пару целиком. Третий
застаёт то, что первые два пропустили: группы, ставшие супергруппой до 23.09.2026,
когда бот переезду ещё не умел, и апдейты, потерянные поллером. Тот же ответ
Telegram даёт и на `getChat` — на этом построена проверка `probe_basic_groups`:
в тихом чате первого уведомления можно ждать неделями. Все пути ведут в `follow`,
и она идемпотентна: кто пришёл вторым, застаёт переезд сделанным.

До этого модуля переезда не было вовсе: новая супергруппа регистрировалась
незаявленным чатом без привязок, бот отвечал в ней «чат не подключён», а
уведомления уходили на мёртвый старый `chat_id` и падали с 400.

**Новый чат получает НОВУЮ строку `tg_chats`, а не новый `chat_id` в старой.**
Номер сообщения Telegram уникален только внутри чата, а `chat_ref` входит в ключи
рядом с номером сообщения: `tgsrc-<chat_ref>-<message_id>` — ключ идемпотентности
создания задачи, он живёт тегом на задаче в Битриксе (И-10), — и `tg_message_links`.
Нумерация у супергруппы своя, и сохрани чат свой `chat_ref`, новое сообщение с тем
же номером, что у старого, получило бы «задача уже создана» и чужую задачу. Поэтому
старая строка остаётся надгробием (`status='migrated'`, `migrated_to` — преемник),
а к новой переезжает то, что относится к чату, а не к его сообщениям. Что именно —
`MOVES` и `STAYS` ниже; страж `tests/test_chat_migration.py` сверяет их со схемой.

Строка нового чата при этом бывает уже заведена: первый апдейт из супергруппы
регистрирует её незаявленной (`dispatch._register_chat`) раньше, чем дошла весть о
переезде, — так было со всеми группами до этого модуля. Такая строка ничего не
несёт и становится преемником сама: второй живой строки на один чат не бывает.
Если же новый чат уже заявлен ДРУГИМ теннантом, переезд не делается: данные одного
теннанта не переезжают к другому ни при каком раскладе (И-2).
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import asyncpg

from b24bot.crypto import box
from b24bot.db.pool import pool, system_scope
from b24bot.domain import audit
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

# Откуда узнали о переезде: пишется в журнал действий и в лог, чтобы разбор
# «почему привязки переехали» не начинался с догадок.
FROM_UPDATE = "служебное сообщение Telegram"
FROM_SEND = "ответ Telegram на отправку"
FROM_PROBE = "проверка обычной группы (getChat)"

MOVED, ALREADY, UNKNOWN, CONFLICT = "moved", "already", "unknown", "conflict"

LOCK_TIMEOUT_MS = 15_000
# Сколько секунд проверка обычных групп может занимать воркер: он один, и
# уведомления всё это время ждут. Недоделанное доделает следующий проход.
PROBE_BUDGET = 30.0

# Каждая таблица, ссылающаяся на tg_chats(id), и что с ней при переезде. Страж
# сверяет оба словаря с каталогом базы: новая ссылка на чат, не названная здесь,
# валит сборку — иначе её строки молча остались бы у надгробия.
MOVES = {
    "chat_bindings": "привязки проектов — вместе с id, а значит, и с настройками "
                     "уведомлений уровня привязки",
    "tg_topics": "темы форума",
    "outbox": "неотправленное (pending и sending): у старой группы упрётся в 400",
    "callback_tokens": "живые кнопки: кнопки уведомлений из очереди выданы заранее",
}
STAYS = {
    "tg_message_links": "номера сообщений старой группы — у новой нумерация своя",
    "survey_sessions": "вопрос остался в старой группе, реплай на него из новой "
                       "невозможен; активные отменяются",
}
# Ссылка без внешнего ключа — `reminder_marks` со scope='chat' (scope_id — это
# chat_ref): отметки утренней сводки переезжают, иначе в день переезда чат
# получил бы вторую сводку. Каталог такую ссылку не видит, она названа здесь.


class Moved(NamedTuple):
    """Сколько чего переехало к новому чату (опросы — сколько прервано)."""

    bindings: int = 0
    outbox: int = 0
    tokens: int = 0
    surveys: int = 0


@dataclass(frozen=True)
class Result:
    outcome: str                 # moved | already | unknown | conflict
    old_ref: int | None = None
    new_ref: int | None = None
    tenant_id: int | None = None
    title: str = ""
    moved: Moved = field(default_factory=Moved)


def carried_status(status: str, tenant_id: int | None) -> str:
    """Состояние, с которым чат продолжает жить в супергруппе.

    Метка `left` снимается так же, как при возвращении бота в чат
    (`dispatch._register_chat`): весть о переезде пришла боту, значит, он в чате.
    """
    if status == "left":
        return "active" if tenant_id is not None else "unclaimed"
    return status


def _lock_key(old_chat_id: int) -> int:
    raw = f"tgchat-migrate:{old_chat_id}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


async def follow(old_chat_id: int, new_chat_id: int, *, bot_ref: int | None,
                 source: str, title: str | None = None,
                 is_forum: bool | None = None) -> Result:
    """Перевести чат со старого `chat_id` на новый. Повтор ничего не меняет.

    Системный слой (RLS), как и регистрация чатов: строка нового чата до переезда
    бывает ничьей, а решение «чужой ли это чат» требует видеть её владельца.
    Скоуп объявляется здесь, а не у вызывающих: входов несколько, и забыть его в
    очередном значило бы получить переезд, который не видит половины строк.

    `title` и `is_forum` — то, что известно из самого апдейта; ответ Telegram на
    отправку или `getChat` их не несёт, и тогда берётся название старой группы, а
    признак форума обновит первый же апдейт из новой (`dispatch._register_chat`).
    """
    if old_chat_id == new_chat_id:
        return Result(UNKNOWN)
    with system_scope():
        try:
            async with pool().acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
                # Обе вести, ответ на отправку и боты разных теннантов в одной
                # группе приходят параллельно. Лок транзакционный, а не сессионный —
                # по той же причине, что в b24/tokens.py (И-5): его не забыть при
                # отмене задачи.
                await conn.execute("SELECT pg_advisory_xact_lock($1)",
                                   _lock_key(old_chat_id))
                result = await _follow(conn, old_chat_id, new_chat_id, bot_ref=bot_ref,
                                       title=title, is_forum=is_forum)
        except asyncpg.RaiseError as exc:
            # Триггер «один чат — один клиент»: новый чат уже привязан к проекту
            # другого клиента. Это решение человека, принятое в новом чате, —
            # старые привязки его не перекрывают. Транзакция откатилась целиком.
            log.warning("переезд группы в супергруппу отменён: в новом чате проекты "
                        "другого клиента (%s)", str(exc)[:200])
            return Result(CONFLICT)

        _report(result, source)
        if result.outcome == MOVED and result.tenant_id is not None:
            # После фиксации: запись о переезде, которого не было, хуже её
            # отсутствия.
            await audit.record(
                result.tenant_id, "chat.migrate", actor_kind="system",
                target=f"chat:{result.new_ref}",
                detail={"источник": source, "из": f"chat:{result.old_ref}",
                        "чат": result.title, "привязок": result.moved.bindings,
                        "уведомлений в очереди": result.moved.outbox,
                        "кнопок": result.moved.tokens,
                        "опросов прервано": result.moved.surveys})
    return result


async def _follow(conn: Any, old_chat_id: int, new_chat_id: int, *,
                  bot_ref: int | None, title: str | None,
                  is_forum: bool | None) -> Result:
    old = await conn.fetchrow(
        "SELECT id, tenant_id, status, title, bot_ref, first_seen_at, claimed_at "
        "FROM tg_chats WHERE chat_id = $1 AND status <> 'migrated' FOR UPDATE",
        old_chat_id)
    if old is None:
        return await _sweep(conn, old_chat_id, new_chat_id)

    tenant_id = old["tenant_id"]
    name = title or old["title"] or ""
    status = carried_status(old["status"], tenant_id)

    new_ref: int | None = None
    new = await _live(conn, new_chat_id)
    if new is None:
        new_ref = await conn.fetchval(
            "INSERT INTO tg_chats (chat_id, tenant_id, bot_ref, type, title, is_forum, "
            "status, first_seen_at, claimed_at) "
            "VALUES ($1, $2, $3, 'supergroup', $4, $5, $6, $7, $8) "
            "ON CONFLICT (chat_id) WHERE status <> 'migrated' DO NOTHING RETURNING id",
            new_chat_id, tenant_id, old["bot_ref"] or bot_ref, name, bool(is_forum),
            status, old["first_seen_at"], old["claimed_at"])
        if new_ref is None:
            # Новый чат зарегистрировался сам, пока мы здесь: апдейт из него
            # разбирал другой бот той же группы.
            new = await _live(conn, new_chat_id)

    if new is not None:
        if (tenant_id is not None and new["tenant_id"] is not None
                and new["tenant_id"] != tenant_id):
            log.warning("переезд группы в супергруппу отменён: новый чат %s уже "
                        "заявлен другим теннантом, старый %s остаётся как есть",
                        new["id"], old["id"])
            return Result(CONFLICT, old_ref=old["id"], new_ref=new["id"])
        new_ref = int(new["id"])
        # Строка, заведённая первым апдейтом из супергруппы, ничья: ей
        # достаётся заявка старой. Решение, уже принятое в новом чате, — главнее.
        await conn.execute(
            """
            UPDATE tg_chats
               SET tenant_id     = COALESCE(tenant_id, $2::bigint),
                   status        = CASE WHEN tenant_id IS NULL AND $2::bigint IS NOT NULL
                                             AND status <> 'left'
                                        THEN $3::text ELSE status END,
                   claimed_at    = COALESCE(claimed_at, $4::timestamptz),
                   first_seen_at = LEAST(first_seen_at, $5::timestamptz),
                   type          = 'supergroup',
                   is_forum      = COALESCE($6::boolean, is_forum)
             WHERE id = $1
            """, new_ref, tenant_id, status, old["claimed_at"], old["first_seen_at"],
            is_forum)
    if new_ref is None:
        raise RuntimeError(f"строка нового чата {new_chat_id} не заводится и не находится")

    moved = (await _move(conn, tenant_id, old["id"], new_ref)
             if tenant_id is not None else Moved())
    await conn.execute(
        "UPDATE tg_chats SET status = 'migrated', migrated_to = $2 WHERE id = $1",
        old["id"], new_ref)
    return Result(MOVED, old_ref=old["id"], new_ref=new_ref, tenant_id=tenant_id,
                  title=name, moved=moved)


async def _live(conn: Any, chat_id: int) -> Any:
    return await conn.fetchrow(
        "SELECT id, tenant_id, status FROM tg_chats "
        "WHERE chat_id = $1 AND status <> 'migrated' FOR UPDATE", chat_id)


async def _sweep(conn: Any, old_chat_id: int, new_chat_id: int) -> Result:
    """Переезд уже сделан. Дочистить то, что успело встать к надгробию.

    Воркер мог поставить уведомление в очередь старого чата за миг до переезда:
    рассылка прочла его живым. Такая строка упёрлась бы в 400 навсегда, а повтор
    переноса ничего лишнего не трогает — он идемпотентен.
    """
    tomb = await conn.fetchrow(
        """
        SELECT m.id, m.tenant_id, m.title, c.id AS new_ref, c.chat_id AS new_chat_id,
               c.tenant_id AS new_tenant
          FROM tg_chats m JOIN tg_chats c ON c.id = m.migrated_to
         WHERE m.chat_id = $1 AND m.status = 'migrated' AND c.status <> 'migrated'
         ORDER BY m.id DESC LIMIT 1
        """, old_chat_id)
    if tomb is None:
        return Result(UNKNOWN)  # старую группу бот не видел вовсе — переносить нечего
    done = Result(ALREADY, old_ref=tomb["id"], new_ref=tomb["new_ref"],
                  tenant_id=tomb["tenant_id"], title=tomb["title"] or "")
    if tomb["new_chat_id"] != new_chat_id:
        # Группа становится супергруппой один раз. Расхождение — не повод
        # переносить что-то куда-то ещё, но повод посмотреть глазами.
        log.warning("весть о переезде чата %s → %s расходится с записанной: "
                    "преемник %s", old_chat_id, new_chat_id, tomb["new_chat_id"])
        return done
    if tomb["tenant_id"] is None or tomb["new_tenant"] != tomb["tenant_id"]:
        return done  # у ничейного чата переносить нечего, к чужому — нельзя
    moved = await _move(conn, tomb["tenant_id"], tomb["id"], tomb["new_ref"])
    return Result(ALREADY, old_ref=done.old_ref, new_ref=done.new_ref,
                  tenant_id=done.tenant_id, title=done.title, moved=moved)


async def _move(conn: Any, tenant_id: int, old_ref: int, new_ref: int) -> Moved:
    """Перенести к новому чату всё, что относится к чату, а не к сообщениям.

    Каждый запрос — строго в пределах теннанта старой строки (И-2): переезд
    идёт в системном скоупе, и RLS здесь не страхует.
    """
    # Темы — первыми: привязка к теме ссылается на неё по id, и сверка дублей
    # ниже честна, только когда темы уже лежат у нового чата. У обычной группы
    # тем не бывает, но строку, названную в MOVES, переносит только этот код.
    await conn.execute(
        """
        UPDATE tg_topics t SET chat_ref = $3
         WHERE t.tenant_id = $1 AND t.chat_ref = $2
           AND NOT EXISTS (SELECT 1 FROM tg_topics x
                            WHERE x.chat_ref = $3 AND x.thread_id = t.thread_id)
        """, tenant_id, old_ref, new_ref)
    # Привязка переезжает вместе со своим id: настройки уведомлений уровня
    # привязки ключуются именно им (notifications.py) и едут вместе с ней.
    bindings = await conn.fetch(
        """
        UPDATE chat_bindings b SET chat_ref = $3
         WHERE b.tenant_id = $1 AND b.chat_ref = $2
           AND NOT EXISTS (SELECT 1 FROM chat_bindings x
                            WHERE x.chat_ref = $3 AND x.project_id = b.project_id
                              AND COALESCE(x.topic_ref, 0) = COALESCE(b.topic_ref, 0))
        RETURNING b.id
        """, tenant_id, old_ref, new_ref)
    # Не переехало только то, что в новом чате уже есть: там его привязали руками,
    # и это решение главнее. Копия у надгробия выключается — иначе экраны
    # приложения показывали бы её живой привязкой несуществующего чата.
    await conn.execute(
        "UPDATE chat_bindings SET status = 'disabled' "
        "WHERE tenant_id = $1 AND chat_ref = $2 AND status <> 'disabled'",
        tenant_id, old_ref)
    outbox = await conn.fetch(
        "UPDATE outbox SET chat_ref = $3 WHERE tenant_id = $1 AND chat_ref = $2 "
        "AND state IN ('pending', 'sending') RETURNING id", tenant_id, old_ref, new_ref)
    tokens = await conn.fetch(
        "UPDATE callback_tokens SET chat_ref = $3 WHERE tenant_id = $1 "
        "AND chat_ref = $2 AND expires_at > now() RETURNING token_hash",
        tenant_id, old_ref, new_ref)
    await conn.execute(
        """
        UPDATE reminder_marks m SET scope_id = $3
         WHERE m.tenant_id = $1 AND m.scope = 'chat' AND m.scope_id = $2
           AND NOT EXISTS (SELECT 1 FROM reminder_marks x
                            WHERE x.tenant_id = $1 AND x.scope = 'chat'
                              AND x.scope_id = $3 AND x.kind = m.kind)
        """, tenant_id, old_ref, new_ref)
    surveys = await conn.fetch(
        "UPDATE survey_sessions SET state = 'cancelled' WHERE tenant_id = $1 "
        "AND chat_ref = $2 AND state = 'active' RETURNING id", tenant_id, old_ref)
    return Moved(len(bindings), len(outbox), len(tokens), len(surveys))


async def probe_basic_groups() -> int:
    """Спросить Telegram об обычных группах: не стали ли они супергруппой.

    Вести о переезде бот мог не услышать: до 23.09.2026 он их не разбирал, поллер
    теряет апдейты (08.09 — `drop_pending_updates`), а ответ на отправку приходит
    лишь с первым уведомлением, которого в тихом чате можно ждать неделями — всё
    это время бот отвечал бы в новом чате «не подключён». `getChat` по старому
    chat_id отвечает той же ошибкой с новым id, что и отправка.

    Спрашиваются только заявленные обычные группы: супергруппа второй раз не
    превращается, и переехавший чат уходит из выборки сам. Порядок случайный,
    время ограничено `PROBE_BUDGET`: групп может оказаться больше, чем влезает
    в один проход, и первые по списку не должны заслонять остальные навсегда.
    Возвращает число переехавших чатов.
    """
    with system_scope():  # обход чатов всех теннантов (RLS)
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.chat_id, c.bot_ref, b.tenant_id, b.bot_id, b.token
                  FROM tg_chats c
                  JOIN tg_bots b ON b.id = c.bot_ref AND b.status = 'active'
                 WHERE c.type = 'group' AND c.status IN ('claimed', 'active')
                   AND c.tenant_id IS NOT NULL
                 ORDER BY random()
                """)
    deadline = time.monotonic() + PROBE_BUDGET
    moved = 0
    for row in rows:
        if time.monotonic() > deadline:
            log.info("проверка обычных групп не уложилась в %.0f с — остальные в "
                     "следующем проходе", PROBE_BUDGET)
            break
        token = box.decrypt(row["token"], box.aad("tg_bots", "token", row["tenant_id"],
                                                  row["bot_id"]))
        try:
            await tg.call(token, "getChat", {"chat_id": int(row["chat_id"])})
        except tg.TelegramError as exc:
            # Бот вышел, группа удалена, сеть легла — это не переезд, и разбирать
            # это здесь некому: переезд — единственное, за чем пришли.
            if exc.migrate_to_chat_id is None:
                continue
            result = await follow(int(row["chat_id"]), exc.migrate_to_chat_id,
                                  bot_ref=int(row["bot_ref"]), source=FROM_PROBE)
            moved += result.outcome == MOVED
    return moved


def _report(result: Result, source: str) -> None:
    m = result.moved
    if result.outcome == MOVED:
        log.info("группа стала супергруппой (%s): чат %s → %s, теннант %s, "
                 "привязок %d, в очереди %d, кнопок %d, опросов прервано %d",
                 source, result.old_ref, result.new_ref, result.tenant_id,
                 m.bindings, m.outbox, m.tokens, m.surveys)
    elif result.outcome == ALREADY and any(m):
        log.info("переезд чата %s → %s уже сделан, дочищены хвосты (%s): "
                 "привязок %d, в очереди %d, кнопок %d, опросов прервано %d",
                 result.old_ref, result.new_ref, source, m.bindings, m.outbox,
                 m.tokens, m.surveys)
