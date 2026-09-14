"""Инструкция в мини-аппе — то же обещание, что меню команд.

Текст лежит данными в `web/miniapp/src/guide/content.ts`, и страж читает его как
текст — так же, как `test_commands.py` читает роутер. Сверяется четыре вещи:

1. каждая `/команда` инструкции есть в реестре бота: названная и несуществующая
   команда читается как «раньше работало и сломали»;
2. каждая команда реестра в инструкции названа: без этого «полная» инструкция
   устаревает молча, с первой же новой командой;
3. каждая [[кнопка бота]] дословно есть в коде бота: переименовали кнопку —
   человек ищет в чате то, чего там нет;
4. каждый {{элемент}} есть в коде приложения, каждый значок — в наборе.

Разметку описывает шапка `content.ts`; разбор здесь обязан совпадать с
`guide/markup.tsx`, иначе страж сверял бы не то, что видит человек.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from b24bot.bot import commands, handlers

ROOT = Path(__file__).resolve().parents[1]
MINIAPP = ROOT / "web" / "miniapp" / "src"
CONTENT = (MINIAPP / "guide" / "content.ts").read_text(encoding="utf-8")

# Строковые литералы TypeScript в одинарных кавычках — весь текст инструкции.
_LITERAL = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
TEXT = "\n".join(m.group(1) for m in _LITERAL.finditer(CONTENT))

_CODE = re.compile(r"`([^`\n]+)`")
_BOT_BUTTON = re.compile(r"\[\[([^\]\n]+)\]\]")
_APP_REF = re.compile(r"\{\{([^}\n]+)\}\}")
_BOLD = re.compile(r"\*\*[^*\n]+\*\*")

# Код бота, где живут подписи кнопок: клавиатуры, обработчики, напоминания,
# подтверждение задач. Подпись ищется ЦЕЛЫМ литералом, а не подстрокой: кнопка
# «Завтра» не должна находиться в слове «Послезавтра».
BOT_SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for folder in ("bot", "domain")
    for path in sorted((ROOT / "src" / "b24bot" / folder).glob("*.py")))

# Код приложения без самой инструкции: иначе каждый {{элемент}} находил бы себя.
APP_SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted(MINIAPP.rglob("*.ts*"))
    if "guide" not in path.relative_to(MINIAPP).parts)

ICONS = (MINIAPP / "ui" / "Icon.tsx").read_text(encoding="utf-8")


def _commands_in_guide() -> set[str]:
    """Имена команд из кусков кода: `/time 233 1ч30м` → time, `/t_233` → t_233."""
    names: set[str] = set()
    for code in _CODE.findall(TEXT):
        names |= set(re.findall(r"(?<![\w/])/([a-z][a-z0-9_]*)", code))
    return names


def test_guide_is_parsed_at_all() -> None:
    """Страж, разобравший пустоту, «проходит» всегда — проверяем, что текст есть."""
    assert len(TEXT) > 5000
    assert len(_BOT_BUTTON.findall(TEXT)) > 30
    assert len(_APP_REF.findall(TEXT)) > 30


def test_every_command_in_the_guide_exists() -> None:
    unknown = sorted(name for name in _commands_in_guide()
                     if name not in commands.NAMES and handlers.card_shortcut(name) is None)
    assert not unknown, f"инструкция называет команды, которых нет в боте: {unknown}"


def test_every_bot_command_is_in_the_guide() -> None:
    missing = sorted(commands.NAMES - _commands_in_guide())
    assert not missing, f"команды есть в боте, но инструкция о них молчит: {missing}"


def test_the_card_shortcut_is_explained() -> None:
    """У сводки уведомлений кнопок нет — карточка оттуда открывается только `/t_<номер>`.

    Инструкция, промолчавшая об этом, оставила бы человека без единого пути
    к задаче из сводки; назвавшая, но без разбора в роутере, — обманула бы.
    """
    shortcuts = [name for name in _commands_in_guide() if name.startswith("t_")]
    assert shortcuts, "инструкция обязана рассказать про /t_<номер>"
    assert all(handlers.card_shortcut(name) is not None for name in shortcuts)


@pytest.mark.parametrize("label", sorted(set(_BOT_BUTTON.findall(TEXT))))
def test_bot_button_exists_verbatim(label: str) -> None:
    assert f'"{label}"' in BOT_SOURCE or f"'{label}'" in BOT_SOURCE, (
        f"в инструкции кнопка бота [[{label}]], а в коде бота такой подписи нет")


@pytest.mark.parametrize("ref", sorted(set(_APP_REF.findall(TEXT))))
def test_app_element_exists(ref: str) -> None:
    label, _, icon = ref.partition("|")
    assert label in APP_SOURCE, f"в инструкции {{{{{label}}}}}, а в приложении такого нет"
    if icon:
        assert re.search(rf"^\s+{re.escape(icon)}: \[", ICONS, re.M), (
            f"значка {icon!r} нет в наборе ui/Icon.tsx")


def test_article_ids_are_unique() -> None:
    """По id работают «Дальше», поиск и вход из других экранов (`openGuide('link')`)."""
    ids = re.findall(r"^\s+id: '([^']+)',\n\s+icon:", CONTENT, re.M)
    assert len(ids) > 10
    assert len(ids) == len(set(ids)), ids


def test_entry_points_open_existing_articles() -> None:
    """Экраны приложения открывают инструкцию на статье по id. Опечатка в id
    открыла бы оглавление вместо ответа — тихо, и никто бы не заметил."""
    ids = set(re.findall(r"^\s+id: '([^']+)',\n\s+icon:", CONTENT, re.M))
    called = set(re.findall(r"openGuide\('([^']+)'\)", APP_SOURCE))
    assert called, "вход в инструкцию на статье пропал из App.tsx"
    assert called <= ids, called - ids


def test_markup_is_balanced() -> None:
    """Непарная скобка не упадёт — она покажется человеку как есть: «[[Сводка»."""
    for literal in (m.group(1) for m in _LITERAL.finditer(CONTENT)):
        stripped = literal
        for pattern in (_CODE, _BOT_BUTTON, _APP_REF, _BOLD):
            stripped = pattern.sub("", stripped)
        for mark in ("[[", "]]", "{{", "}}", "`", "**"):
            assert mark not in stripped, f"непарная разметка {mark!r} в строке: {literal}"
