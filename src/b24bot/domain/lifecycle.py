"""Жизненный цикл портала: установка, подписка Маркета, удаление приложения.

До этого модуля установка заканчивалась записью теннанта, а всё остальное
подразумевалось вечным: подписки на события создавались руками при спайке,
`ONAPPUNINSTALL` не обрабатывался вовсе, подписку Маркета не проверял никто.
Для одного своего портала это работало; для тиражного приложения каждая из этих
дыр — сломанный портал, который никто не заметит.

Три правила, на которых всё держится:

1. **И-8 буквально.** `APPLICATION_TOKEN` видит любой сотрудник портала, поэтому
   ни удаление, ни ротация токена не выполняются по одному событию. Событие —
   повод для дозапроса СВОИМ токеном: `ONAPPUNINSTALL` подтверждается тем, что
   наш токен перестал работать; `ONAPPUPDATE` — тем, что `app.info` показывает
   другую версию.
2. **Блокирует только достоверный сигнал** (правило из mclick). Функциональность
   выключается, когда портал прямо сказал «платный статус и не оплачено».
   Ошибка `app.info`, пустой статус, локальное приложение — всё это НЕ блокирует:
   ложная блокировка платящего клиента дороже ложного пропуска.
3. **Удаление данных — отложенное.** Модерация Маркета требует удалять данные
   портала при деинсталляции; переустановка в тот же день — обычное дело.
   Поэтому деинсталляция помечает (`uninstalled_at`), а стирает воркер через
   `PURGE_AFTER`; переустановка до срока снимает пометку и всё сохраняет.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from b24bot.b24 import errors
from b24bot.b24.limiter import Lane
from b24bot.b24.tokens import NeedsReauth
from b24bot.core.config import get_settings
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import access, audit

log = logging.getLogger(__name__)

# Шесть событий задач (docs/00-portal-facts.md §11) плюс два события жизненного
# цикла — те самые «восемь нужных подписок» с пилота. Для тиражного приложения
# Битрикс шлёт ONAPP* и без подписки, но `event.bind` идемпотентен, а локальному
# приложению (пилоту) подписка необходима — поэтому список один на оба случая.
REQUIRED_EVENTS: tuple[str, ...] = (
    "ONTASKADD", "ONTASKUPDATE", "ONTASKDELETE",
    "ONTASKCOMMENTADD", "ONTASKCOMMENTUPDATE", "ONTASKCOMMENTDELETE",
    "ONAPPUNINSTALL", "ONAPPUPDATE",
)

EVENTS_PATH = "/b24/events"

# Окно на переустановку. До истечения все данные на месте и переустановка
# возвращает портал в строй как был; после — данные стираются насовсем.
# Срок обещан пользователю в лицензионном соглашении (/legal) — менять только
# вместе с ним.
PURGE_AFTER = timedelta(days=30)

# Как часто перепроверять подписку и состав подписок на события. app.info — один
# вызов; сутки — достаточно быстро, чтобы заметить истёкшую подписку, и достаточно
# редко, чтобы не заметить в лимитах.
LICENSE_TTL = timedelta(hours=20)
DAILY_BATCH = 5

# Статусы, у которых существует оплата. L (локальное) и F (бесплатное) не
# блокируются никогда; D (демо) — это модератор Маркета, блокировать его — значит
# провалить модерацию с формулировкой «приложение не работает».
PAID_STATUSES = frozenset({"T", "P", "S"})
KNOWN_STATUSES = frozenset({"L", "F", "D", "T", "P", "S"})

_TRUE_WORDS = frozenset({"Y", "YES", "TRUE", "1"})


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------- app.info
@dataclass(frozen=True)
class AppInfo:
    status: str
    payment_expired: bool
    version: int | None
    days: int | None


def parse_app_info(result: Any) -> AppInfo:
    """`app.info` разбирается либерально: Битрикс непоследователен в типах.

    Числа приходят то числом, то строкой, булево — то 'Y'/'N', то true/false
    (наблюдение mclick, `app_info.go`). Нераспознанное значение трактуется в
    сторону «не блокировать» — правило 2 из докстринга модуля.
    """
    data = result if isinstance(result, dict) else {}
    status = str(data.get("STATUS") or "").strip().upper()

    raw_expired = data.get("PAYMENT_EXPIRED")
    if isinstance(raw_expired, bool):
        expired = raw_expired
    else:
        expired = str(raw_expired or "").strip().upper() in _TRUE_WORDS

    def _int(value: Any) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    return AppInfo(status=status, payment_expired=expired,
                   version=_int(data.get("VERSION")), days=_int(data.get("DAYS")))


def is_blocked(status: str | None, payment_expired: bool) -> bool:
    """То же правило, что в генерированной колонке `tenants.license_blocked`.

    Дублирование с SQL сознательное и проверяется тестом: колонка нужна фильтрам
    воркера, функция — коду, который держит `AppInfo` в руках до записи.
    """
    return (status or "").upper() in PAID_STATUSES and payment_expired


async def blocked(tenant_id: int) -> bool:
    """Выключена ли функциональность теннанта подпиской. Читает колонку."""
    async with pool().acquire() as conn:
        value = await conn.fetchval(
            "SELECT license_blocked FROM tenants WHERE id = $1", tenant_id)
    return bool(value)


async def _fetch_app_info(tenant_id: int) -> AppInfo:
    client = await access.client_for_service(tenant_id)
    async with client:
        result = await client.call("app.info", lane=Lane.BACKGROUND)
    return parse_app_info(result)


async def refresh_license(tenant_id: int) -> AppInfo:
    """Перечитать `app.info` и записать снимок. Бросает исключения портала.

    `license_checked_at` ставится только при успехе: «проверено» с провалившейся
    проверкой — это то самое молчаливое расхождение с порталом, против которого
    у справочников заведены отметки времени.
    """
    info = await _fetch_app_info(tenant_id)
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET b24_app_status = $2, license_expired = $3, "
            "b24_app_version = COALESCE($4, b24_app_version), "
            "license_checked_at = now(), updated_at = now() WHERE id = $1",
            tenant_id, info.status or None, info.payment_expired, info.version)
    if is_blocked(info.status, info.payment_expired):
        log.warning("подписка Маркета истекла: теннант %s, статус %s",
                    tenant_id, info.status)
    return info


async def note_app_status(tenant_id: int, status: str | None) -> None:
    """Буква статуса из placement/install-POST — бесплатный свежий сигнал.

    Пишется ТОЛЬКО статус: `license_expired` и `license_checked_at` не трогаются,
    иначе «проверено» появилось бы без проверки (приём из mclick,
    `SaveAppStatusOnInstall`). Неизвестная буква не пишется вовсе — это чужой
    ввод из POST-а, а не ответ портала нашему токену.
    """
    letter = (status or "").strip().upper()
    if letter not in KNOWN_STATUSES:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET b24_app_status = $2, updated_at = now() "
            "WHERE id = $1 AND b24_app_status IS DISTINCT FROM $2",
            tenant_id, letter)


# ------------------------------------------------------------ подписки событий
def events_handler_url() -> str:
    return get_settings().public_base_url.rstrip("/") + EVENTS_PATH


async def ensure_event_bindings(tenant_id: int) -> tuple[int, int]:
    """Досоздать недостающие подписки. Возвращает (создано сейчас, всего нужно).

    Сверка идёт по паре «событие + наш обработчик»: чужие подписки этого же
    портала (другие приложения не видны, но подписки нашего приложения на другой
    адрес после смены домена — видны) не считаются нашими. `event.bind`
    идемпотентен, отказ одной подписки не валит остальные — суточный проход
    досоздаст (best-effort, как в mclick).
    """
    handler = events_handler_url()
    client = await access.client_for_service(tenant_id)
    async with client:
        bound: set[str] = set()
        existing = await client.call("event.get", lane=Lane.BACKGROUND)
        for row in existing if isinstance(existing, list) else []:
            if not isinstance(row, dict):
                continue
            if str(row.get("handler") or "").rstrip("/") == handler.rstrip("/"):
                bound.add(str(row.get("event") or "").upper())

        created = 0
        for event in REQUIRED_EVENTS:
            if event in bound:
                continue
            try:
                await client.call("event.bind",
                                  {"event": event, "handler": handler},
                                  lane=Lane.BACKGROUND)
                created += 1
            except errors.B24Error as exc:
                log.warning("event.bind %s не прошёл (теннант %s): %s",
                            event, tenant_id, exc)
    if created:
        log.info("подписки на события досозданы: теннант %s, %d шт.",
                 tenant_id, created)
    return created, len(REQUIRED_EVENTS)


# --------------------------------------------------------------- деинсталляция
async def on_uninstall_event(tenant_id: int) -> bool:
    """`ONAPPUNINSTALL`: подтвердить своим токеном и пометить теннанта.

    Подтверждение обратное обычному (И-8): подделать «приложение удалено» можно
    только сломав наш токен, а это и есть удаление. `app.info` живым токеном
    отвечает — значит приложение стоит и событие поддельное. Токен мёртв
    (`NeedsReauth` при обновлении или отказ портала токену) — удаление настоящее.

    Неоднозначный исход (сеть, лимиты) не делает НИЧЕГО: суточный проход увидит
    мёртвый токен позже, а ложная деинсталляция стоила бы теннанту остановки
    поллера и, через 30 дней, всех данных.
    """
    try:
        await _fetch_app_info(tenant_id)
    except (NeedsReauth, errors.B24AuthError):
        pass  # токен мёртв — удаление подтверждено
    except Exception as exc:
        log.warning("ONAPPUNINSTALL не подтверждён (теннант %s): %s — не трогаю",
                    tenant_id, str(exc)[:200])
        return False
    else:
        log.warning("поддельный ONAPPUNINSTALL: app.info отвечает, теннант %s "
                    "не тронут (И-8)", tenant_id)
        return False

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE tenants SET status = 'uninstalled', uninstalled_at = now(), "
            "updated_at = now() WHERE id = $1 AND status = 'active' RETURNING id",
            tenant_id)
        await conn.execute(
            "UPDATE b24_user_tokens SET state = 'revoked' "
            "WHERE tenant_id = $1 AND state = 'active'", tenant_id)
    if row is None:
        return True  # повторное событие: уже помечен, делать нечего
    await audit.record(tenant_id, "tenant.uninstall", actor_kind="system",
                       detail={"источник": "ONAPPUNINSTALL",
                               "удаление данных": f"через {PURGE_AFTER.days} дн."})
    log.warning("приложение удалено с портала: теннант %s помечен, данные "
                "будут удалены через %d дн.", tenant_id, PURGE_AFTER.days)
    return True


async def on_update_event(tenant_id: int, new_app_token: str | None,
                          granted_scope: str | None) -> bool:
    """`ONAPPUPDATE`: подтвердить смену версии и принять новый APPLICATION_TOKEN.

    Обновление приложения РОТИРУЕТ `APPLICATION_TOKEN` и снимает подписки на
    события (docs/50-web-and-b24-app.md §2.1). Не обработать его — значит молча
    перестать узнавать входящие события и запросы placement: оба сверяются со
    старым токеном.

    Подтверждение — `app.info` СВОИМ токеном: версия отличается от записанной —
    обновление настоящее. Совпадает — событие поддельное, токен не трогается:
    подложный токен в `tenants` глушил бы настоящие события до переустановки.
    """
    try:
        info = await _fetch_app_info(tenant_id)
    except Exception as exc:
        log.warning("ONAPPUPDATE не подтверждён (теннант %s): %s — не трогаю",
                    tenant_id, str(exc)[:200])
        return False

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT b24_app_version, b24_member_id FROM tenants WHERE id = $1",
            tenant_id)
    if row is None:
        return False
    stored = row["b24_app_version"]
    if info.version is not None and stored is not None and int(stored) == info.version:
        log.warning("поддельный ONAPPUPDATE: версия %s не менялась, теннант %s "
                    "не тронут (И-8)", stored, tenant_id)
        return False

    async with pool().acquire() as conn:
        if new_app_token:
            enc = box.encrypt(new_app_token,
                              box.aad("tenants", "b24_app_token", tenant_id,
                                      str(row["b24_member_id"])))
            await conn.execute(
                "UPDATE tenants SET b24_app_token = $2, b24_app_token_kid = $3, "
                "updated_at = now() WHERE id = $1", tenant_id, enc, box.kid_of(enc))
        if granted_scope:
            await conn.execute(
                "UPDATE tenants SET granted_scope = $2, updated_at = now() "
                "WHERE id = $1", tenant_id, granted_scope.split(","))
        await conn.execute(
            "UPDATE tenants SET b24_app_status = COALESCE($2, b24_app_status), "
            "license_expired = $3, b24_app_version = COALESCE($4, b24_app_version), "
            "license_checked_at = now(), updated_at = now() WHERE id = $1",
            tenant_id, info.status or None, info.payment_expired, info.version)

    try:
        await ensure_event_bindings(tenant_id)
    except Exception as exc:
        log.warning("пересоздание подписок после ONAPPUPDATE не прошло "
                    "(теннант %s): %s — досоздаст суточный проход",
                    tenant_id, str(exc)[:200])
    await audit.record(tenant_id, "tenant.app_update", actor_kind="system",
                       detail={"версия": info.version,
                               "токен обновлён": bool(new_app_token)})
    log.info("приложение обновлено на портале: теннант %s, версия %s",
             tenant_id, info.version)
    return True


# --------------------------------------------------------------------- чистка
async def purge_due() -> int:
    """Стереть данные теннантов, у которых окно переустановки истекло.

    `DELETE FROM tenants` делает остальное каскадом: каждая доменная таблица
    ссылается на `tenants` с `ON DELETE CASCADE` (проверено по миграциям).
    Единственное исключение — партиционированный `audit_log` без FK, он чистится
    явно. Полнота стирания проверяется тестом, который обходит все таблицы с
    колонкой `tenant_id`, — новая таблица без каскада не проскочит молча.

    В аудит запись не пишется: журнал этого теннанта стирается тем же проходом,
    а чужому теннанту про это знать нечего. След остаётся в логах приложения.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, slug, b24_domain FROM tenants "
            "WHERE status = 'uninstalled' AND uninstalled_at < now() - $1::interval",
            PURGE_AFTER)
    purged = 0
    for row in rows:
        tid = int(row["id"])
        async with pool().acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM audit_log WHERE tenant_id = $1", tid)
            await conn.execute("DELETE FROM tenants WHERE id = $1", tid)
        purged += 1
        log.warning("данные теннанта %s (%s, %s) удалены: %d дн. после "
                    "деинсталляции истекли", tid, row["slug"], row["b24_domain"],
                    PURGE_AFTER.days)
    return purged


# --------------------------------------------------------------- суточный ход
async def daily_pass() -> None:
    """Регулярный проход: чистка просроченных, подписка и события по кругу.

    Портал, который поставили и не трогают, иначе не проверялся бы никогда —
    а именно у него первым протухает подписка (наблюдение mclick). Не больше
    `DAILY_BATCH` теннантов за проход: каждый стоит 1–2 вызовов против
    operating-лимита, и проход обязан заканчиваться быстро.
    """
    try:
        await purge_due()
    except Exception:
        log.exception("чистка деинсталлированных теннантов не прошла")

    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM tenants WHERE status = 'active' AND "
            "(license_checked_at IS NULL OR license_checked_at < now() - $1::interval) "
            "ORDER BY license_checked_at ASC NULLS FIRST LIMIT $2",
            LICENSE_TTL, DAILY_BATCH)
    for row in rows:
        tid = int(row["id"])
        try:
            await refresh_license(tid)
            await ensure_event_bindings(tid)
        except NeedsReauth:
            log.warning("суточный проход: у теннанта %s нет живого сервисного "
                        "токена — подписка и события не проверены", tid)
        except Exception as exc:
            log.warning("суточный проход не прошёл для теннанта %s: %s",
                        tid, str(exc)[:200])
