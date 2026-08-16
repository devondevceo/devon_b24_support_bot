"""Шифрование секретов. Инвариант И-7 и защита от переноса токена между теннантами."""
from __future__ import annotations

import re

import pytest

from b24bot.crypto import box

STORED_RE = re.compile(r"^enc:[0-9]+:[0-9]+:[A-Za-z0-9_-]+$")


def ad(tenant: int = 7, user: int = 42) -> bytes:
    return box.aad("b24_user_tokens", "refresh_token", tenant, user)


def test_roundtrip() -> None:
    enc = box.encrypt("секретный refresh", ad())
    assert box.decrypt(enc, ad()) == "секретный refresh"


def test_stored_format_matches_db_domain_check() -> None:
    """Формат обязан проходить CHECK домена enc_text из миграции 0001."""
    assert STORED_RE.match(box.encrypt("x", ad()))


def test_aad_blocks_cross_tenant_copy() -> None:
    """Главное свойство: строку теннанта 7 нельзя расшифровать как строку теннанта 8.

    Без AAD зашифрованный refresh_token можно было бы просто скопировать между
    строками таблицы, и он бы расшифровался.
    """
    enc = box.encrypt("секрет теннанта 7", ad(tenant=7))
    with pytest.raises(box.DecryptError):
        box.decrypt(enc, ad(tenant=8))


def test_aad_blocks_column_swap() -> None:
    """И между колонками тоже: access_token нельзя подставить вместо refresh_token."""
    enc = box.encrypt("значение", box.aad("b24_user_tokens", "access_token", 7, 42))
    with pytest.raises(box.DecryptError):
        box.decrypt(enc, box.aad("b24_user_tokens", "refresh_token", 7, 42))


def test_nonce_is_unique_per_encryption() -> None:
    values = {box.encrypt("одно и то же", ad()) for _ in range(20)}
    assert len(values) == 20, "повтор nonce в AES-GCM разрушает шифрование"


def test_kid_is_readable_without_key() -> None:
    """Джоб ротации обязан находить строки по kid, не расшифровывая их."""
    assert box.kid_of(box.encrypt("x", ad())) == 1


def test_corrupted_value_raises() -> None:
    enc = box.encrypt("x", ad())
    with pytest.raises(box.DecryptError):
        box.decrypt(enc[:-4] + "AAAA", ad())
    with pytest.raises(box.DecryptError):
        box.decrypt("не наш формат", ad())


def test_redact_never_leaks_full_secret() -> None:
    out = box.redact("8123456:AAHverysecrettokenvalue")
    assert "verysecret" not in out
    assert "23 симв" in out or "симв" in out
