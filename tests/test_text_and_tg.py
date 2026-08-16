"""Экранирование (И-6) и проверки токена Telegram."""
from __future__ import annotations

import pytest

from b24bot.core.text import esc_attr, esc_bbcode, esc_html, safe_filename
from b24bot.tg import api as tg


# ------------------------------------------------------------------ И-6: HTML
def test_esc_html_neutralises_fake_system_line() -> None:
    """Имя пользователя в Telegram задаёт он сам — и может подделать системную строку."""
    evil = '</b><a href="https://evil">✅ Подтверждено администратором</a>'
    out = esc_html(evil)
    assert "<a href" not in out
    assert "&lt;" in out


def test_esc_html_order_does_not_double_encode() -> None:
    assert esc_html("a & b < c") == "a &amp; b &lt; c"
    assert esc_html("&lt;") == "&amp;lt;"


def test_esc_attr_closes_quotes() -> None:
    assert '"' not in esc_attr('" onmouseover="alert(1)')


# --------------------------------------------------------------- И-6: BBCode
def test_esc_bbcode_kills_fake_official_link() -> None:
    """Название группы приходит из Битрикса и тоже подставляется в описание задачи."""
    evil = "[/b][url=https://evil.example]Открыть в Битрикс24[/url][b]"
    out = esc_bbcode(evil)
    assert "[url=" not in out
    assert "[/b]" not in out
    # Текст сохранён целиком, просто обезврежен: пользователь не должен терять содержимое.
    assert "Открыть в Битрикс24" in out


def test_esc_bbcode_strips_control_chars() -> None:
    assert "\x00" not in esc_bbcode("текст\x00с нулём")


# ----------------------------------------------------------- имена файлов
def test_safe_filename_removes_bidi_masking() -> None:
    """U+202E маскирует расширение: «отчет‮gpj.exe» выглядит как «отчетexe.jpg»."""
    masked = "отчет‮gpj.exe"
    out = safe_filename(masked)
    assert "‮" not in out
    assert out.endswith(".exe"), "настоящее расширение обязано остаться видимым"


def test_safe_filename_drops_newlines_and_trims() -> None:
    assert "\n" not in safe_filename("имя\nс переносом.txt")
    long = safe_filename("я" * 300 + ".pdf")
    assert len(long) <= 102
    assert long.endswith(".pdf"), "расширение важнее хвоста имени"


def test_safe_filename_never_returns_empty() -> None:
    assert safe_filename("") == "файл"
    assert safe_filename("‮‭") == "файл"


# ------------------------------------------------------------------ Telegram
@pytest.mark.parametrize("token,ok", [
    ("123456789:AAHverylongtokenvaluewith30plus", True),
    ("123456789AAH", False),
    ("abc:AAHverylongtokenvaluewith30pluschars", False),
    ("123:short", False),
    ("", False),
])
def test_token_looks_valid(token: str, ok: bool) -> None:
    """Грубая проверка формы до сетевого вызова: не тратим запрос на явный мусор."""
    assert tg.token_looks_valid(token) is ok


def test_bot_id_from_token() -> None:
    assert tg.bot_id_from_token("8123456:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") == 8123456
    assert tg.bot_id_from_token("мусор") is None


def test_allowed_updates_has_no_reactions() -> None:
    """message_reaction не запрашиваем: сценария нет, а трафик и записи в очередь — есть."""
    assert "message_reaction" not in tg.ALLOWED_UPDATES
    assert "my_chat_member" in tg.ALLOWED_UPDATES
    assert "chat_member" in tg.ALLOWED_UPDATES


# --------------------------------------------------------------- прокси
def test_proxy_scheme_is_forced_to_remote_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """socks5 -> socks5h. Локальный резолв даёт заблокированный адрес и таймаут.

    Замерено на боевом сервере: socks5h — ответ за 0.5 с, socks5 — таймаут 20 с.
    """
    from b24bot.core.config import Settings

    s = Settings(database_url="postgresql://x@h/db",
                 master_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
                 tg_proxy_url="socks5://user:pass@1.2.3.4:1080")
    assert s.tg_proxy == "socks5h://user:pass@1.2.3.4:1080"


def test_proxy_untouched_when_already_remote_or_http() -> None:
    from b24bot.core.config import Settings

    base = {"database_url": "postgresql://x@h/db",
            "master_key": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"}
    assert Settings(**base, tg_proxy_url="socks5h://h:1").tg_proxy == "socks5h://h:1"
    assert Settings(**base, tg_proxy_url="http://h:3128").tg_proxy == "http://h:3128"
    assert Settings(**base, tg_proxy_url="  ").tg_proxy is None


def test_long_poll_gets_longer_http_timeout() -> None:
    """HTTP-таймаут обязан превышать длительность long polling.

    Иначе httpx рвёт соединение раньше ответа Telegram, и цикл вырождается
    в серию таймаутов — ровно это и случилось на боевом сервере.
    """
    assert tg.http_timeout_for("getUpdates", {"timeout": 25}) > 25
    assert tg.http_timeout_for("getMe", None) == tg.TIMEOUT
    assert tg.http_timeout_for("getUpdates", {}) == tg.TIMEOUT


# ------------------------------------------------------------------- BBCode
def test_bbcode_to_text_strips_markup() -> None:
    """Портал хранит описания и комментарии в BBCode. Человеку нужен текст."""
    from b24bot.core.text import bbcode_to_text

    raw = ("[b]— Источник —[/b]\nTelegram: чат «Поддержка»\n"
           "[i]— из Telegram, Иван[/i]")
    out = bbcode_to_text(raw)
    assert "[b]" not in out and "[/i]" not in out
    assert "— Источник —" in out
    assert "чат «Поддержка»" in out


def test_bbcode_keeps_link_address() -> None:
    """Выкинуть адрес вместе с тегом — значит потерять содержимое."""
    from b24bot.core.text import bbcode_to_text

    out = bbcode_to_text("см. [url=https://t.me/c/1/2]сообщение[/url]")
    assert out == "см. сообщение (https://t.me/c/1/2)"


def test_bbcode_does_not_touch_escaped_brackets() -> None:
    """Экранированные esc_bbcode скобки — уже не разметка, а текст человека."""
    from b24bot.core.text import bbcode_to_text, esc_bbcode

    text = esc_bbcode("смотри [b]тут[/b]")
    assert bbcode_to_text(text) == text
