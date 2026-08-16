"""Вложения: извлечение, лимиты, блок-лист."""
from __future__ import annotations

from b24bot.tg import files


def test_extract_picks_largest_photo() -> None:
    """Telegram присылает лестницу размеров — берём самый крупный."""
    msg = {"message_id": 7, "photo": [
        {"file_id": "small", "file_size": 1000},
        {"file_id": "big", "file_size": 90000},
    ]}
    out = files.extract(msg)
    assert len(out) == 1
    assert out[0].file_id == "big"
    assert out[0].name.endswith(".jpg")


def test_extract_normalises_document_name() -> None:
    """U+202E маскирует расширение: «отчет‮gpj.exe» выглядит как jpg."""
    msg = {"message_id": 1,
           "document": {"file_id": "d", "file_name": "отчет\u202egpj.exe",
                        "file_size": 100}}
    out = files.extract(msg)
    assert "\u202e" not in out[0].name
    assert out[0].name.endswith(".exe")


def test_blocked_extensions() -> None:
    for name in ("вирус.exe", "script.JS", "install.msi", "run.bat", "a.lnk"):
        assert files.is_blocked(name), name
    for name in ("отчёт.pdf", "скрин.png", "данные.xlsx", "без_расширения"):
        assert not files.is_blocked(name), name


def test_extract_handles_message_without_files() -> None:
    assert files.extract({"message_id": 1, "text": "просто текст"}) == []


def test_limits_are_bot_api_reality() -> None:
    """20 МБ — потолок скачивания у Bot API, обойти его без своего сервера нельзя."""
    assert files.MAX_SIZE == 20 * 1024 * 1024
    assert files.MAX_PER_TASK == 10
