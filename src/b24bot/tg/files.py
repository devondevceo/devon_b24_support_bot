"""Скачивание файлов из Telegram.

Лимит Bot API на скачивание — 20 МБ, и обойти его нельзя без своего Bot API сервера
(решение заказчика: остаёмся на облачном). Видео с телефона перекрывает лимит
регулярно, поэтому отказ должен быть внятным, а не молчаливым.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from b24bot.core.config import TELEGRAM_HOST, get_settings
from b24bot.core.text import safe_filename
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

MAX_SIZE = 20 * 1024 * 1024
MAX_PER_TASK = 10
TOTAL_PER_TASK = 60 * 1024 * 1024

# Мы не антивирус, но переносить исполняемые файлы из недоверенного чата в
# корпоративный Диск клиента не будем: бот стал бы транспортом доставки.
BLOCKED_EXT = {"exe", "scr", "bat", "cmd", "com", "pif", "ps1", "js", "jse", "vbs",
               "vbe", "wsf", "lnk", "msi", "hta", "jar", "reg", "cpl"}


@dataclass
class Attachment:
    file_id: str
    name: str
    size: int
    kind: str


class FileTooBig(Exception):
    def __init__(self, name: str, size: int) -> None:
        self.name, self.size = name, size
        super().__init__(f"{name}: {size} байт")


class FileBlocked(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


def extract(msg: dict[str, Any]) -> list[Attachment]:
    """Вложения сообщения. Имена нормализуются сразу (bidi, переводы строк)."""
    out: list[Attachment] = []

    doc = msg.get("document")
    if isinstance(doc, dict):
        out.append(Attachment(str(doc["file_id"]),
                              safe_filename(doc.get("file_name") or "документ"),
                              int(doc.get("file_size") or 0), "document"))

    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        # Telegram присылает лестницу размеров — берём самый крупный.
        best = max(photos, key=lambda p: int(p.get("file_size") or 0))
        out.append(Attachment(str(best["file_id"]),
                              f"photo_{msg.get('message_id', 0)}.jpg",
                              int(best.get("file_size") or 0), "photo"))

    for key, ext in (("video", "mp4"), ("voice", "ogg"), ("audio", "mp3"),
                     ("video_note", "mp4")):
        node = msg.get(key)
        if isinstance(node, dict):
            name = node.get("file_name") or f"{key}_{msg.get('message_id', 0)}.{ext}"
            out.append(Attachment(str(node["file_id"]), safe_filename(name),
                                  int(node.get("file_size") or 0), key))
    return out


def is_blocked(name: str) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in BLOCKED_EXT


async def download(token: str, att: Attachment) -> bytes:
    """Скачать файл. Проверки размера — до и после запроса пути."""
    if att.size and att.size > MAX_SIZE:
        raise FileTooBig(att.name, att.size)
    if is_blocked(att.name):
        raise FileBlocked(att.name)

    info = await tg.call(token, "getFile", {"file_id": att.file_id})
    path = str(info.get("file_path") or "")
    size = int(info.get("file_size") or att.size or 0)
    if size > MAX_SIZE:
        raise FileTooBig(att.name, size)
    if not path:
        raise FileTooBig(att.name, size)

    url = f"https://{TELEGRAM_HOST}/file/bot{token}/{path}"
    async with httpx.AsyncClient(timeout=120, proxy=get_settings().tg_proxy) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.content
