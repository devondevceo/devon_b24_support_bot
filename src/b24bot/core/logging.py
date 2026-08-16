"""Логирование со скраббером. Инвариант И-7: секреты никогда не попадают в логи.

Поводом послужил живой случай: httpx на уровне INFO пишет полный URL запроса, а у
Telegram Bot API токен находится прямо в пути (`/bot<id>:<secret>/getUpdates`). Через
минуту после запуска токен бота лежал в json-логах контейнера на диске.

Скраббер работает на уровне logging.Filter, то есть чистит ВСЁ, что попадает в лог,
включая сообщения сторонних библиотек, которые про наши правила ничего не знают.
"""
from __future__ import annotations

import logging
import re
from typing import Any

# Токен бота в URL: /bot123456789:AA.../method
_TG_TOKEN = re.compile(r"/bot(\d{6,}):[A-Za-z0-9_\-]{20,}")
# auth=<токен Битрикса> в query или теле
_B24_AUTH = re.compile(r"(auth=)[A-Za-z0-9._\-]{16,}")
# access_token / refresh_token / client_secret в любом виде
_TOKEN_KV = re.compile(
    r"((?:access_token|refresh_token|client_secret|secret_token|application_token)"
    r"['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9._\-]{12,})")
# Телефоны с разделителями: наивная \+?\d{10,15} не ловит «8 999 123-45-67»,
# то есть ровно ту форму, в которой человек пишет номер в чат.
_PHONE = re.compile(r"(?<![\w.])(?:\+?\d[\s\-()]{0,2}){10,15}(?![\w.])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def scrub(text: str) -> str:
    text = _TG_TOKEN.sub(r"/bot\1:<СКРЫТО>", text)
    text = _B24_AUTH.sub(r"\1<СКРЫТО>", text)
    text = _TOKEN_KV.sub(r"\1<СКРЫТО>", text)
    text = _PHONE.sub("<ТЕЛЕФОН>", text)
    return _EMAIL.sub("<EMAIL>", text)


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: scrub(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    scrub(a) if isinstance(a, str) else a for a in record.args)
        return True


def setup(level: str = "INFO", **_: Any) -> None:
    """Единая настройка логирования для всех процессов сервиса."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s",'
        '"msg":"%(message)s"}'))
    handler.addFilter(ScrubFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # httpx на INFO печатает полный URL каждого запроса. Скраббер это чистит, но
    # шум остаётся: одна строка на каждый long-poll, то есть строка каждые 25 секунд
    # на каждого бота. Ошибки при этом видеть надо.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
