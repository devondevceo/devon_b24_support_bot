"""Кто открыл мини-апп и что ему видно.

Три уровня доверия, и путать их нельзя:

1. **`initData`** доказывает, что это конкретный telegram-аккаунт и что открыто
   через конкретного бота. Проверяется HMAC-ом токеном этого бота.
2. **`startapp`** доказывает, из какого чата пришли: значение выдали мы сами и
   положили в `callback_tokens`. Из самого `initData` чат узнать нельзя — Telegram
   присылает лишь `chat_type` и `chat_instance`, а по ним чат не опознать.
3. **Личный токен Битрикса** решает, что человеку можно делать. Права режет портал,
   мы их не расширяем.

Инвариант И-3 не ослабляется мини-аппом: доступ к задаче идёт через
`authorize_task_for_chat` с тем же единственным текстом отказа. Мини-апп добавляет
лишь способ выбрать чат в личке — и после выбора он ведёт себя как обычный чат.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Any

from b24bot.core.config import get_settings
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import access
from b24bot.domain.context import ChatContext, ProjectRef, load_chat_context_by_ref
from b24bot.tg import initdata

log = logging.getLogger(__name__)

# Незнакомый аккаунт в личке нельзя привязать к теннанту заранее — теннант станет
# известен только после успешной сверки подписи. Перебор ботов ограничен: это
# редкий путь (человек ещё не привязан), а тратить на него десятки расшифровок
# на каждый запрос нельзя.
MAX_BLIND_CANDIDATES = 25


class Unauthenticated(Exception):
    """Подпись не сошлась. Наружу — один и тот же отказ, без подробностей."""


class NotLinked(Exception):
    """Аккаунт опознан, но не сопоставлен с пользователем Битрикса."""

    def __init__(self, tenant_id: int, bot_username: str) -> None:
        self.tenant_id, self.bot_username = tenant_id, bot_username
        super().__init__("telegram не привязан к пользователю Битрикс24")


class Forbidden(Exception):
    """Объект есть, но не в этом чате или не у этого теннанта."""


@dataclass(frozen=True)
class Actor:
    tenant_id: int
    tg_user_id: int
    b24_user_id: int
    bot_username: str
    init: initdata.InitData


@dataclass(frozen=True)
class Bot:
    tenant_id: int
    bot_id: int
    username: str
    token: str


# ------------------------------------------------------------------- кандидаты
async def _bot_of_tenant(tenant_id: int) -> Bot | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id, bot_id, username, token FROM tg_bots "
            "WHERE tenant_id = $1 AND status <> 'suspended'", tenant_id)
    return _bot_of(row)


def _bot_of(row: Any) -> Bot | None:
    if row is None:
        return None
    token = box.decrypt(row["token"],
                        box.aad("tg_bots", "token", row["tenant_id"], row["bot_id"]))
    return Bot(int(row["tenant_id"]), int(row["bot_id"]),
               str(row["username"] or ""), token)


async def _candidates(tg_user_id: int | None, start_param: str | None) -> list[Bot]:
    """Боты, чьими токенами имеет смысл пробовать сверку подписи.

    Порядок важен только для скорости: подпись сходится ровно с одним токеном,
    и именно она, а не этот список, решает, кто перед нами.
    """
    tenant_ids: list[int] = []

    unpacked = unpack_context(start_param) if start_param else None
    if unpacked is not None:
        async with pool().acquire() as conn:
            owner = await conn.fetchval("SELECT tenant_id FROM tg_chats WHERE id = $1",
                                        unpacked.chat_ref)
        if owner is not None:
            tenant_ids.append(int(owner))

    if tg_user_id is not None:
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.tenant_id FROM tenant_members m "
                "JOIN users u ON u.id = m.user_id "
                "JOIN tenants t ON t.id = m.tenant_id AND t.status = 'active' "
                "WHERE u.tg_user_id = $1", tg_user_id)
        tenant_ids += [int(r["tenant_id"]) for r in rows]

    seen: set[int] = set()
    bots: list[Bot] = []
    for tid in tenant_ids:
        if tid in seen:
            continue
        seen.add(tid)
        bot = await _bot_of_tenant(tid)
        if bot is not None:
            bots.append(bot)

    if bots:
        return bots

    # Никого не нашли: человек открыл бота в личке и ещё не привязан.
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT b.tenant_id, b.bot_id, b.username, b.token FROM tg_bots b "
            "JOIN tenants t ON t.id = b.tenant_id AND t.status = 'active' "
            "WHERE b.status <> 'suspended' ORDER BY b.id LIMIT $1",
            MAX_BLIND_CANDIDATES)
    return [b for b in (_bot_of(r) for r in rows) if b is not None]


# ------------------------------------------------------------- аутентификация
async def authenticate(raw_init: str) -> Actor:
    """Проверить `initData` и понять, кто это. Единственная дверь в мини-апп."""
    if not raw_init or len(raw_init) > 8192:
        raise Unauthenticated("пустая или неправдоподобно длинная строка")

    peeked_user = initdata.peek_user_id(raw_init)
    peeked_start = initdata.peek_start_param(raw_init)

    for bot in await _candidates(peeked_user, peeked_start):
        try:
            data = initdata.verify(raw_init, bot.token)
        except initdata.InitDataInvalid:
            continue

        b24_user_id = await access.linked_b24_user(bot.tenant_id, data.user_id)
        if b24_user_id is None:
            raise NotLinked(bot.tenant_id, bot.username)
        return Actor(tenant_id=bot.tenant_id, tg_user_id=data.user_id,
                     b24_user_id=b24_user_id, bot_username=bot.username, init=data)

    log.info("мини-апп: подпись не сошлась ни с одним ботом")
    raise Unauthenticated("подпись не проверена")


# -------------------------------------------------------------------- контекст
@dataclass(frozen=True)
class Packed:
    chat_ref: int
    thread_id: int | None
    task_id: int | None


_PACKED_RE = re.compile(r"^(\d+)-(\d+)-(\d+)-([0-9a-f]{16})$")
SHORT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,29}$")


def _sign(payload: str) -> str:
    key = get_settings().master_key_bytes
    return hmac.new(key, f"miniapp:{payload}".encode(), hashlib.sha256).hexdigest()[:16]


def pack_context(chat_ref: int, thread_id: int | None = None,
                 task_id: int | None = None) -> str:
    """Значение для `startapp`: чат, из которого открывают мини-апп.

    Подписанная строка, а не строка в `callback_tokens`, по двум причинам. Первая:
    ссылка живёт под каждым сообщением бота и в закреплённом меню — записи в базу
    на каждую отрисовку не нужны. Вторая: наружу уходит случайное значение, а в
    базе лежит его sha256, поэтому одну и ту же ссылку из базы не восстановить,
    и каждая отрисовка порождала бы новую строку.

    Доступа значение не даёт: оно лишь называет чат. Кто перед нами, решает подпись
    Telegram, а что ему можно — личный токен Битрикса.
    """
    payload = f"{chat_ref}-{thread_id or 0}-{task_id or 0}"
    return f"{payload}-{_sign(payload)}"


def unpack_context(value: str) -> Packed | None:
    """Разобрать `startapp`. None означает «подделка или мусор» — молча и одинаково."""
    match = _PACKED_RE.match(value or "")
    if match is None:
        return None
    chat_ref, thread_id, task_id, signature = match.groups()
    payload = f"{chat_ref}-{thread_id}-{task_id}"
    if not hmac.compare_digest(_sign(payload), signature):
        log.warning("мини-апп: подпись startapp не сошлась")
        return None
    return Packed(int(chat_ref), int(thread_id) or None, int(task_id) or None)


def deep_link(bot_username: str, short_name: str, packed: str) -> str:
    """Прямая ссылка на мини-апп.

    Кнопки `web_app` в инлайн-клавиатуре Telegram разрешает только в личке, поэтому
    из группового чата мини-апп открывается ссылкой вида `t.me/<бот>/<имя>?startapp=`.
    Имя приложения заводится в BotFather командой `/newapp`.
    """
    return f"https://t.me/{bot_username}/{short_name}?startapp={packed}"


def web_app_url() -> str | None:
    """Адрес мини-аппа для кнопок `web_app` — они работают ТОЛЬКО в личке.

    Здесь короткое имя из BotFather не нужно вовсе: кнопка несёт сам URL. Telegram
    требует https, поэтому на локальном http кнопки просто не будет.
    """
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/miniapp" if base.startswith("https://") else None


async def short_name_of(tenant_id: int) -> str:
    """Имя мини-аппа теннанта. Пусто — значит мини-апп не заведён и ссылок не будет."""
    async with pool().acquire() as conn:
        value = await conn.fetchval(
            "SELECT miniapp_short_name FROM tg_bots WHERE tenant_id = $1", tenant_id)
    name = str(value or "") or get_settings().miniapp_short_name
    return name if SHORT_NAME_RE.match(name) else ""


START_PREFIX = "a"  # /start a<подписанный контекст> — открыть мини-апп в личке


async def link_for_chat(tenant_id: int, chat_ref: int, *, thread_id: int | None = None,
                        task_id: int | None = None) -> str | None:
    """Ссылка на мини-апп для кнопки в ГРУППОВОМ чате.

    Два пути, и оба работают без ручной настройки чата:

    * если у бота заведено короткое имя приложения (`/newapp` в BotFather) —
      прямая ссылка, мини-апп открывается в одно касание;
    * если нет — `t.me/<бот>?start=a<контекст>`. Человек попадает в личку бота, и
      бот тут же присылает кнопку `web_app` с тем же контекстом. На касание больше,
      зато ноль ручных шагов у теннанта.

    Кнопку `web_app` прямо в группу поставить нельзя: Telegram разрешает их только
    в личных чатах, и это ограничение клиента, а не наше решение.
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username, miniapp_short_name FROM tg_bots WHERE tenant_id = $1 "
            "AND status <> 'suspended'", tenant_id)
    if row is None or not row["username"]:
        return None
    if web_app_url() is None:
        return None  # мини-апп не развёрнут: ссылка вела бы в никуда

    username = str(row["username"])
    packed = pack_context(chat_ref, thread_id, task_id)
    short = str(row["miniapp_short_name"] or "") or get_settings().miniapp_short_name
    if SHORT_NAME_RE.match(short):
        return deep_link(username, short, packed)
    return f"https://t.me/{username}?start={START_PREFIX}{packed}"


def web_app_url_for(packed: str | None = None) -> str | None:
    """Адрес страницы мини-аппа, при необходимости с контекстом чата.

    Контекст уезжает в query, а не в `startapp`: значение подписано нашим ключом,
    и проверяем мы его сами — Telegram здесь ничего не гарантирует ни в том, ни в
    другом случае.
    """
    base = web_app_url()
    return f"{base}?ctx={packed}" if base and packed else base


@dataclass(frozen=True)
class Context:
    chat_ref: int
    title: str
    projects: list[ProjectRef]
    thread_id: int | None
    pinned: bool  # пришёл из подписанного startapp, менять его нельзя
    task_id: int | None = None


async def resolve_context(actor: Actor, *, chat_ref: int | None = None,
                          packed_ctx: str | None = None) -> Context | None:
    """Чат, в котором работает мини-апп.

    Три источника, по убыванию доверия:

    1. `startapp` из подписи Telegram — прямая ссылка из группы;
    2. `ctx` в адресе страницы — та же подписанная строка, но пришедшая через
       кнопку `web_app` в личке (в неё `startapp` положить нельзя);
    3. `chat_ref` числом — выбор чата руками в личке.

    Первые два подписаны нашим ключом, третий проверяется принадлежностью чата
    теннанту. Во всех случаях итог один: дальше работает обычная проверка И-3.
    """
    signed = actor.init.start_param or packed_ctx
    if signed:
        packed = unpack_context(signed)
        if packed is None:
            raise Forbidden("ссылка не распознана")
        return await _context_of(actor, packed.chat_ref, packed.thread_id,
                                 pinned=True, task_id=packed.task_id)

    if chat_ref is None:
        return None
    return await _context_of(actor, chat_ref, None, pinned=False)


async def _context_of(actor: Actor, chat_ref: int, thread_id: Any, *, pinned: bool,
                      task_id: Any = None) -> Context:
    ctx = await load_chat_context_by_ref(chat_ref,
                                         int(thread_id) if thread_id else None)
    if ctx is None or ctx.tenant_id != actor.tenant_id:
        log.warning("мини-апп: чат %s недоступен теннанту %s", chat_ref, actor.tenant_id)
        raise Forbidden("чат недоступен")
    return Context(chat_ref=ctx.chat_ref, title=ctx.title, projects=ctx.projects,
                   thread_id=ctx.thread_id, pinned=pinned,
                   task_id=int(task_id) if task_id else None)


async def contexts_for(actor: Actor, visible_groups: set[int] | None) -> list[dict[str, Any]]:
    """Чаты теннанта с привязками — список для выбора в личке.

    Фильтр по группам портала (`visible_groups`) отсекает проекты, которых человек
    и так не видит в Битриксе. Это не замена проверке прав — её делает портал на
    каждой выборке задач, — а способ не показывать список, в котором всё пусто.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id AS chat_ref, c.title, c.status,
                   p.id AS project_id, p.b24_group_id, p.name AS project,
                   cl.name AS client
              FROM chat_bindings b
              JOIN tg_chats c ON c.id = b.chat_ref AND c.status IN ('claimed','active')
              JOIN projects  p ON p.id = b.project_id AND p.status = 'active'
              JOIN clients  cl ON cl.id = p.client_id
             WHERE b.tenant_id = $1 AND b.status = 'active'
             ORDER BY c.title, p.name
            """, actor.tenant_id)

    by_chat: dict[int, dict[str, Any]] = {}
    for r in rows:
        gid = int(r["b24_group_id"])
        if visible_groups is not None and gid not in visible_groups:
            continue
        entry = by_chat.setdefault(int(r["chat_ref"]), {
            "chat_ref": int(r["chat_ref"]),
            "title": r["title"] or "чат без названия",
            "projects": [],
        })
        entry["projects"].append({"id": int(r["project_id"]), "name": r["project"],
                                  "client": r["client"], "b24_group_id": gid})
    return list(by_chat.values())


def as_chat_context(ctx: Context, tenant_id: int) -> ChatContext:
    """Переходник к общему коду бота: он умеет работать с ChatContext."""
    return ChatContext(chat_ref=ctx.chat_ref, chat_id=0, title=ctx.title,
                       status="active", tenant_id=tenant_id, is_forum=False,
                       thread_id=ctx.thread_id, projects=list(ctx.projects))
