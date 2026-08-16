"""Скраббер логов. Инвариант И-7.

Поводом послужил живой случай: httpx на INFO печатает полный URL, а токен бота
у Telegram лежит прямо в пути. Через минуту после запуска токен был в логах.
"""
from __future__ import annotations

import logging

from b24bot.core.logging import ScrubFilter, scrub

# Форма настоящая, значения синтетические: тест проверяет скраббер, а не хранит
# секрет. Живой токен здесь означал бы, что И-7 нарушает файл, который его защищает.
REAL_SHAPE = "/bot1234567890:AAHnQwErTyUiOpAsDfGhJkLzXcVbNm12345/getUpdates"


def test_telegram_token_never_survives() -> None:
    out = scrub(f"HTTP Request: POST https://api.telegram.org{REAL_SHAPE}")
    assert "AAHnQwEr" not in out
    assert "<СКРЫТО>" in out
    # ID бота оставляем: по нему разбирают инциденты, и секретом он не является.
    assert "1234567890" in out


def test_b24_auth_param_scrubbed() -> None:
    out = scrub("POST https://devondev.bitrix24.ru/rest/user.current?auth=abc123def456ghi789")
    assert "abc123def456" not in out


def test_token_key_values_scrubbed() -> None:
    for text in ('access_token: "s3cr3tvaluelong123"',
                 "refresh_token=s3cr3tvaluelong123",
                 'client_secret":"QwErTyUiOpAsDfGhJkLz"'):
        assert "s3cr3tvalue" not in scrub(text)
        assert "QwErTyUiOpAs" not in scrub(text)


def test_russian_phone_forms_are_caught() -> None:
    r"""Наивная \+?\d{10,15} не ловит именно те формы, которыми пишут люди."""
    for phone in ("+7 (999) 123-45-67", "8 999 123-45-67", "+79991234567"):
        assert "999" not in scrub(f"клиент оставил номер {phone} для связи")


def test_email_scrubbed() -> None:
    assert "ceo@devondev.ru" not in scrub("написал ceo@devondev.ru по задаче")


def test_filter_cleans_args_too(caplog: object) -> None:
    """Секрет чаще приезжает через %s-аргумент, а не в самой строке формата."""
    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                               "запрос %s", (f"https://api.telegram.org{REAL_SHAPE}",), None)
    ScrubFilter().filter(record)
    assert "AAHnQwEr" not in record.getMessage()


def test_ordinary_text_untouched() -> None:
    msg = "поллер запущен: @devon_sd_bot (теннант 1), задач в проекте 33: 4"
    assert scrub(msg) == msg
