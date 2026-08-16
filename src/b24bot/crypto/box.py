"""Шифрование секретов. Инвариант И-7: секреты только в шифрованных колонках.

Формат хранения:  enc:2:<kid>:<base64url(nonce || ciphertext || tag)>

AAD обязателен и равен "table:column:tenant_id:natural_key". Без него зашифрованное
значение можно скопировать из строки одного теннанта в строку другого, и оно
расшифруется — то есть шифрование не защищало бы от подмены.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from b24bot.core.config import get_settings

FORMAT_VERSION = 2
NONCE_LEN = 12


class DecryptError(Exception):
    """Значение не расшифровывается: не тот ключ, не тот AAD или повреждение."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def aad(table: str, column: str, tenant_id: int | None, natural_key: str | int) -> bytes:
    return f"{table}:{column}:{tenant_id or 0}:{natural_key}".encode()


def encrypt(plaintext: str, ad: bytes, *, key: bytes | None = None,
            kid: int | None = None) -> str:
    s = get_settings()
    key = key or s.master_key_bytes
    kid = kid if kid is not None else s.master_key_id
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), ad)
    return f"enc:{FORMAT_VERSION}:{kid}:{_b64e(nonce + ct)}"


def decrypt(stored: str, ad: bytes, *, key: bytes | None = None) -> str:
    try:
        prefix, ver, _kid, payload = stored.split(":", 3)
    except ValueError as exc:
        raise DecryptError("неизвестный формат шифрованного значения") from exc
    if prefix != "enc" or ver != str(FORMAT_VERSION):
        raise DecryptError(f"неподдерживаемый формат: {prefix}:{ver}")

    raw = _b64d(payload)
    nonce, ct = raw[:NONCE_LEN], raw[NONCE_LEN:]
    try:
        return AESGCM(key or get_settings().master_key_bytes).decrypt(nonce, ct, ad).decode()
    except Exception as exc:  # InvalidTag и всё остальное
        raise DecryptError("не удалось расшифровать значение") from exc


def kid_of(stored: str) -> int | None:
    parts = stored.split(":", 3)
    return int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None


def redact(value: str | None, keep: int = 4) -> str:
    """Для логов и спайк-дампов: показать длину и хвост, но не значение."""
    if not value:
        return "<пусто>"
    return f"<{len(value)} симв., …{value[-keep:]}>"
