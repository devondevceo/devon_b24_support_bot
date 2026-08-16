"""Ошибки Битрикс24, разложенные по классам реакции."""
from __future__ import annotations


class B24Error(Exception):
    def __init__(self, code: str, description: str = "", method: str = "") -> None:
        self.code = code
        self.description = description
        self.method = method
        super().__init__(f"{method}: {code} {description}".strip())


class B24Transport(B24Error):
    """Сеть, таймаут, неразобранный ответ. Ретраить можно."""


class B24QueryLimit(B24Error):
    """503 QUERY_LIMIT_EXCEEDED — переполнено ведро 2 запроса/с. Ретраить с backoff."""


class B24OperatingLimit(B24Error):
    """429 OPERATION_TIME_LIMIT — выбран бюджет operating (420 с в окне 600 с).

    Ретраить бессмысленно раньше сброса окна: подождать до operating_reset_at.
    """


class B24AuthError(B24Error):
    """Токен мёртв: expired_token, invalid_token, invalid_grant, NO_AUTH_FOUND.

    Ретрай тем же токеном бесполезен. Нужен refresh, а при его отказе — needs_reauth.
    """


class B24AccessDenied(B24Error):
    """Прав нет. Это ответ Битрикса пользователю, показываем как есть, не интерпретируем."""


class B24NotFound(B24Error):
    """Объект не найден или недоступен — для Битрикса это часто одно и то же."""


AUTH_CODES = {"expired_token", "invalid_token", "invalid_grant", "NO_AUTH_FOUND",
              "WRONG_AUTH_TYPE", "ACCESS_DENIED_FOR_TOKEN"}
ACCESS_CODES = {"ACCESS_DENIED", "INSUFFICIENT_RIGHTS", "TASKS_ACTION_NOT_ALLOWED"}
NOTFOUND_CODES = {"ERROR_NOT_FOUND", "ITEM_NOT_FOUND_OR_NOT_ACCESSIBLE",
                  "TASKS_TASK_NOT_FOUND", "ERROR_TASK_NOT_FOUND"}


def classify(code: str, description: str = "", method: str = "",
             http_status: int | None = None) -> B24Error:
    """Один вход для превращения ответа портала в исключение нужного класса."""
    c = (code or "").strip()
    if http_status == 503 or c == "QUERY_LIMIT_EXCEEDED":
        return B24QueryLimit(c or "QUERY_LIMIT_EXCEEDED", description, method)
    if http_status == 429 or c == "OPERATION_TIME_LIMIT":
        return B24OperatingLimit(c or "OPERATION_TIME_LIMIT", description, method)
    if c in AUTH_CODES:
        return B24AuthError(c, description, method)
    if c in ACCESS_CODES:
        return B24AccessDenied(c, description, method)
    if c in NOTFOUND_CODES:
        return B24NotFound(c, description, method)
    return B24Error(c or "UNKNOWN", description, method)
