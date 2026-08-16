"""Экранирование подстановок. Инвариант И-6.

Правило без исключений: КАЖДАЯ подстановка в текст проходит через фильтр. Включая
имена людей, названия чатов, имена файлов и данные, пришедшие из Битрикса.

Почему «включая данные из Битрикса»: злоумышленник, переименовав группу в
`[/b][url=https://evil.example]Открыть в Битрикс24[/url][b]`, получает фальшивую
«официальную» ссылку в описании задачи, по которой пойдёт сотрудник поддержки.
Проверено записью: квадратные скобки в тексте Битрикс съедает как BBCode.
"""
from __future__ import annotations

import re

# Управляющие символы направления письма. В имени файла они маскируют расширение:
# "отчет‮gpj.exe" выглядит как "отчетexe.jpg".
BIDI_CHARS = "‪‫‬‭‮⁦⁧⁨⁩"
_BIDI_RE = re.compile(f"[{BIDI_CHARS}]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def esc_html(value: object) -> str:
    """Для сообщений Telegram с parse_mode=HTML."""
    return (str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def esc_attr(value: object) -> str:
    """Для значений HTML-атрибутов на страницах приложения."""
    return esc_html(value).replace('"', "&quot;").replace("'", "&#39;")


def esc_bbcode(value: object) -> str:
    """Для описаний и комментариев в Битриксе.

    Скобки заменяются на похожие символы, а не удаляются: иначе текст пользователя
    молча теряет содержимое, и он этого не поймёт.
    """
    text = _CONTROL_RE.sub("", str(value))
    return text.replace("[", "［").replace("]", "］")


def safe_filename(name: object, limit: int = 100) -> str:
    """Имя файла из недоверенного источника: без bidi, без переводов строк, обрезанное."""
    text = _BIDI_RE.sub("", str(name))
    text = _CONTROL_RE.sub("", text).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        stem, dot, ext = text.rpartition(".")
        if dot and len(ext) <= 8:
            keep = max(1, limit - len(ext) - 2)
            text = f"{stem[:keep]}…{dot}{ext}"
        else:
            text = text[:limit] + "…"
    return text or "файл"
