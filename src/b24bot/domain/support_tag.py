"""Тег поддержки: пометка задач, заведённых через бота, и отбор по ней.

Одна настройка на теннанта (`tenants.support_tag`, миграция `0017`), по умолчанию
`tg-support`. Тег дописывается КАЖДОЙ задаче, созданной через любую нашу дверь —
чат, `/task`, опросник, полная форма мини-аппа, — рядом с ключом идемпотентности.

Зачем: в одном проекте Битрикса живёт и работа по поддержке, и всё остальное.
Отличить одно от другого «по автору» или «по описанию» нельзя, а по тегу — можно,
причём **на стороне портала**: `filter[TAG]` работает вместе с `filter[GROUP_ID]`
(docs/00-portal-facts.md §5.4). Отсюда и берётся разрез трудозатрат «поддержка».

Пустая строка в настройке — это «не помечать вовсе», а не «не настроено». Тогда
задачи создаются без тега, а отчёт показывает одну сумму, как до этой работы.
"""
from __future__ import annotations

import logging
from typing import Any

from b24bot.b24 import mapping
from b24bot.db.pool import pool

log = logging.getLogger(__name__)

DEFAULT_TAG = "tg-support"
TAG_MAX = 60

# Запятая режет тег надвое везде, где Битрикс показывает теги списком.
# Переводов строк и табуляции здесь нет намеренно: `normalize` схлопывает всё
# пробельное РАНЬШЕ этой проверки, и запрет на них никогда бы не сработал.
# Проверка, которая по устройству не срабатывает, хуже отсутствующей: она
# создаёт впечатление защиты там, где её нет.
FORBIDDEN = {",", ";"}


class InvalidTag(ValueError):
    """Тег не годится. Текст исключения показывается человеку как есть."""


def normalize(value: str) -> str:
    """Привести введённое к тому виду, в котором тег уедет в портал.

    Пустая строка — законный ответ: она означает «не помечать задачи».
    """
    # Пробельное схлопывается первым: «тег  поддержки» и «тег поддержки» для
    # человека один тег, а для портала были бы два разных. Перенос строки в поле
    # ввода — почти всегда вставка из буфера, а не намерение.
    tag = " ".join(str(value or "").split())
    if not tag:
        return ""
    if len(tag) > TAG_MAX:
        raise InvalidTag(f"тег длиннее {TAG_MAX} символов")
    if any(c in FORBIDDEN for c in tag):
        raise InvalidTag("в теге нельзя использовать запятую и точку с запятой")
    return tag


async def get(tenant_id: int) -> str:
    """Тег теннанта. Пустая строка = пометка выключена."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT support_tag FROM tenants WHERE id = $1", tenant_id)
    return str(row["support_tag"]) if row else DEFAULT_TAG


async def set_tag(tenant_id: int, value: str) -> str:
    """Записать тег. Возвращает то, что реально сохранено."""
    tag = normalize(value)
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET support_tag = $2, updated_at = now() WHERE id = $1",
            tenant_id, tag)
    return tag


async def synced_at(tenant_id: int) -> Any:
    """Когда разовый проход дописал тег старым задачам. None — не было."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT support_tag_synced_at FROM tenants WHERE id = $1", tenant_id)
    return row["support_tag_synced_at"] if row else None


async def mark_synced(tenant_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET support_tag_synced_at = now() WHERE id = $1", tenant_id)


# ------------------------------------------------------------------ теги задачи
def titles(raw: Any) -> list[str]:
    """Названия тегов задачи из ответа портала.

    Разбор один на весь проект — `mapping.parse_tags`: `TAGS` приходит ОБЪЕКТОМ
    `{"7": {"id": 7, "title": "…"}}`, а не списком (docs/00-portal-facts.md §9.2).
    """
    return mapping.parse_tags(raw)


def has(raw: Any, tag: str) -> bool:
    """Помечена ли задача этим тегом. Пустой тег не помечает ничего."""
    return bool(tag) and tag in titles(raw)


def merged(raw: Any, tag: str) -> list[str] | None:
    """Полный набор тегов задачи вместе с `tag`, или None — если он уже там.

    `tasks.task.update` с `TAGS` задаёт набор ЦЕЛИКОМ (§5.4). Отправить один
    новый тег значит стереть ключ идемпотентности и сломать И-10: повтор того же
    сообщения создал бы вторую задачу. Поэтому дозапись только объединением.
    """
    if not tag:
        return None
    existing = titles(raw)
    return None if tag in existing else [*existing, tag]
