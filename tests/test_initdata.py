"""Проверка подписи initData — единственное удостоверение в мини-аппе.

Цена ошибки здесь максимальная: пройденная сверка означает «это тот telegram-аккаунт
и он открыл нашего бота». Поэтому тесты бьют по всем способам её обойти, а не по
счастливому пути.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from b24bot.core.logging import scrub
from b24bot.domain import miniapp
from b24bot.tg import initdata

TOKEN = "123456789:AAHfake-token-for-tests-only-0000000"
OTHER_TOKEN = "987654321:BBanother-token-for-tests-000000000"


def make(**overrides: object) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAH1234567890",
        "user": json.dumps({"id": 42, "first_name": "Иван", "last_name": "Иванов",
                            "username": "ivanov"}, ensure_ascii=False),
    }
    fields.update({k: str(v) for k, v in overrides.items()})
    return initdata.sign(fields, TOKEN)


def test_valid_signature_gives_user() -> None:
    data = initdata.verify(make(), TOKEN)
    assert data.user_id == 42
    assert data.username == "ivanov"
    assert data.full_name == "Иван Иванов"


def test_wrong_bot_token_rejected() -> None:
    """Подпись сходится ровно с одним ботом — на этом держится выбор теннанта."""
    with pytest.raises(initdata.InitDataInvalid):
        initdata.verify(make(), OTHER_TOKEN)


def test_tampered_field_rejected() -> None:
    raw = make()
    spoiled = raw.replace("%3A+42", "%3A+43")  # подменяем id пользователя в user=…
    assert spoiled != raw
    with pytest.raises(initdata.InitDataInvalid):
        initdata.verify(spoiled, TOKEN)


def test_missing_hash_rejected() -> None:
    with pytest.raises(initdata.InitDataInvalid):
        initdata.verify("auth_date=1&user=%7B%22id%22%3A1%7D", TOKEN)


def test_expired_rejected() -> None:
    old = int((datetime.now(UTC) - timedelta(hours=48)).timestamp())
    with pytest.raises(initdata.InitDataInvalid):
        initdata.verify(make(auth_date=old), TOKEN)


def test_future_auth_date_rejected() -> None:
    ahead = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
    with pytest.raises(initdata.InitDataInvalid):
        initdata.verify(make(auth_date=ahead), TOKEN)


def test_signature_field_participates_in_hash() -> None:
    """Из строки исключается только `hash`.

    Свежие клиенты присылают ещё и `signature` (подпись Ed25519 для сторонней
    проверки). Если выкинуть из подсчёта и её, сверка развалится ровно на них —
    и это будет выглядеть как «у части людей приложение не открывается».
    """
    raw = make(signature="abcdef0123456789")
    assert initdata.verify(raw, TOKEN).user_id == 42


def test_start_param_readable_without_verification() -> None:
    raw = make(start_param="12-0-0-deadbeefdeadbeef")
    assert initdata.peek_start_param(raw) == "12-0-0-deadbeefdeadbeef"
    assert initdata.peek_user_id(raw) == 42


def test_hash_is_scrubbed_from_logs() -> None:
    """Строка initData — секрет: пройденная подпись действует сутки."""
    line = f"запрос: {make()}"
    assert "hash=" in line
    assert "<СКРЫТО>" in scrub(line)
    assert scrub("Authorization: tma query_id=AAA&hash=deadbeef" + "0" * 56).count(
        "<СКРЫТО>") >= 1


# ------------------------------------------------------- подписанный контекст
def test_packed_context_roundtrip() -> None:
    packed = miniapp.pack_context(17, thread_id=None, task_id=195)
    unpacked = miniapp.unpack_context(packed)
    assert unpacked is not None
    assert (unpacked.chat_ref, unpacked.thread_id, unpacked.task_id) == (17, None, 195)


def test_packed_context_rejects_forged_chat() -> None:
    """Подмена чата в ссылке — прямой путь к задачам чужого клиента (И-3)."""
    packed = miniapp.pack_context(17)
    forged = packed.replace("17-", "18-", 1)
    assert miniapp.unpack_context(forged) is None


@pytest.mark.parametrize("value", ["", "мусор", "1-2-3", "1-2-3-короткая",
                                   "1-2-3-" + "0" * 16 + "хвост"])
def test_packed_context_rejects_garbage(value: str) -> None:
    assert miniapp.unpack_context(value) is None
