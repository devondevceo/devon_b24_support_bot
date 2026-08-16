"""Проверка `initData` мини-аппа Telegram.

`initData` — единственное доказательство того, кто открыл мини-апп. Подписывается
Telegram ключом, производным от токена бота, поэтому проверять его можно только
токеном того бота, через которого приложение открыли (docs/40-security.md §7).

Алгоритм Telegram:

    secret_key      = HMAC_SHA256(key="WebAppData", msg=<токен бота>)
    data_check_str  = "\\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")
    hash            = HMAC_SHA256(key=secret_key, msg=data_check_str).hexdigest()

Из строки исключается ТОЛЬКО `hash`. Поле `signature` (подпись Ed25519 для сторонней
проверки без токена) в подсчёт входит: Telegram считает HMAC по всем полям, которые
шлёт, и удаление лишнего поля ломает сверку на всех свежих клиентах.

Строка живёт ровно столько, сколько открыт мини-апп, и заново Telegram её не выдаёт.
Поэтому она одновременно и удостоверение, и срок годности: просроченная означает
«переоткройте приложение», а не «вас разлогинило».
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

log = logging.getLogger(__name__)

# Сутки — компромисс: initData не обновляется, пока приложение открыто, а вкладку
# в Telegram Desktop держат сутками. Утечка строки даёт доступ на тот же срок,
# поэтому в логи она не попадает никогда (её режет скраббер по имени поля).
MAX_AGE = timedelta(hours=24)

SECRET_SALT = b"WebAppData"


class InitDataInvalid(Exception):
    """Строка не прошла проверку. Наружу уходит одинаковый отказ без подробностей."""


@dataclass(frozen=True)
class InitData:
    user_id: int
    username: str
    full_name: str
    start_param: str | None
    auth_date: datetime
    chat_type: str | None
    chat_instance: str | None


def _pairs(raw: str) -> list[tuple[str, str]]:
    return parse_qsl(raw, keep_blank_values=True, strict_parsing=False)


def peek_user_id(raw: str) -> int | None:
    """ID пользователя БЕЗ проверки подписи.

    Нужен ровно для одного: выбрать, токенами каких ботов пробовать сверку.
    Доверять этому значению нельзя — доверие даёт только verify().
    """
    for key, value in _pairs(raw):
        if key != "user":
            continue
        try:
            user = json.loads(value)
        except ValueError:
            return None
        uid = user.get("id") if isinstance(user, dict) else None
        return int(uid) if isinstance(uid, int | str) and str(uid).isdigit() else None
    return None


def peek_start_param(raw: str) -> str | None:
    """`startapp` БЕЗ проверки подписи — тоже только для выбора кандидата."""
    for key, value in _pairs(raw):
        if key == "start_param":
            return value or None
    return None


def verify(raw: str, bot_token: str, *, max_age: timedelta = MAX_AGE,
           now: datetime | None = None) -> InitData:
    """Проверить подпись и срок. Любая неудача — InitDataInvalid без деталей наружу."""
    pairs = _pairs(raw)
    if not pairs:
        raise InitDataInvalid("пустая строка")

    received = next((v for k, v in pairs if k == "hash"), "")
    if not received:
        raise InitDataInvalid("нет hash")

    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")
    secret = hmac.new(SECRET_SALT, bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataInvalid("подпись не совпала")

    data = dict(pairs)
    auth_raw = data.get("auth_date") or ""
    if not auth_raw.isdigit():
        raise InitDataInvalid("нет auth_date")
    auth_date = datetime.fromtimestamp(int(auth_raw), UTC)

    moment = now or datetime.now(UTC)
    if auth_date > moment + timedelta(minutes=5):
        raise InitDataInvalid("auth_date из будущего")
    if moment - auth_date > max_age:
        raise InitDataInvalid("строка просрочена")

    user: dict[str, Any] = {}
    if data.get("user"):
        try:
            parsed = json.loads(data["user"])
        except ValueError as exc:
            raise InitDataInvalid("user не разобрался") from exc
        if isinstance(parsed, dict):
            user = parsed

    uid = user.get("id")
    if not (isinstance(uid, int | str) and str(uid).isdigit()):
        raise InitDataInvalid("нет пользователя")

    name = " ".join(str(user.get(k) or "") for k in ("first_name", "last_name")).strip()
    return InitData(
        user_id=int(uid),
        username=str(user.get("username") or ""),
        full_name=name or "без имени",
        start_param=data.get("start_param") or None,
        auth_date=auth_date,
        chat_type=data.get("chat_type") or None,
        chat_instance=data.get("chat_instance") or None,
    )


def sign(fields: dict[str, str], bot_token: str) -> str:
    """Собрать подписанную строку. Существует ради тестов и только ради них."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(SECRET_SALT, bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    from urllib.parse import urlencode

    return urlencode([*sorted(fields.items()), ("hash", digest)])
