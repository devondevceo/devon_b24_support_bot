"""Общая настройка тестов.

Окружение выставляется ДО импорта модулей приложения: конфигурация читается один раз
и кэшируется, поэтому подменять её потом поздно.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault(
    "MASTER_KEY",
    base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode().rstrip("="),
)
os.environ.setdefault("MASTER_KEY_ID", "1")
os.environ.setdefault("DOMAIN", "b24sdbot.devondev.ru")
os.environ.setdefault("B24_CLIENT_ID", "test.client")
os.environ.setdefault("B24_CLIENT_SECRET", "test.secret")
