"""Реестр слеш-команд.

Смысл этих тестов один: команда, которую человек видит в меню Telegram, обязана
что-то делать. Меню — обещание, и невыполненное обещание выглядит как поломка бота,
а не как «эта команда пока не готова».
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from b24bot.bot import commands

HANDLERS = (Path(__file__).resolve().parents[1]
            / "src" / "b24bot" / "bot" / "handlers.py").read_text(encoding="utf-8")


def _routed_names() -> set[str]:
    """Имена команд, которые реально разбираются роутером."""
    names: set[str] = set()
    for match in re.finditer(r'name (?:==|in) \(?([^):\n]+)\)?:', HANDLERS):
        names |= {m.strip().strip('"\'') for m in match.group(1).split(",")
                  if m.strip().startswith(('"', "'"))}
    for match in re.finditer(r'^\s*(?:slash_actions|action)\s*=\s*\{([^}]+)\}',
                             HANDLERS, re.M):
        names |= set(re.findall(r'"(\w+)":', match.group(1)))
    return names


def test_every_advertised_command_is_handled() -> None:
    """Главный страж: в меню нет ни одной команды, которую роутер не знает."""
    missing = sorted(c.name for c in commands.COMMANDS if c.name not in _routed_names())
    assert not missing, f"команды в меню, но не в роутере: {missing}"


def test_menus_differ_by_scope() -> None:
    """В личке нет команд группы и наоборот: иначе меню обещает невозможное."""
    private = {c["command"] for c in commands.for_scope(commands.PRIVATE)}
    groups = {c["command"] for c in commands.for_scope(commands.GROUPS)}
    admins = {c["command"] for c in commands.for_scope(commands.ADMINS)}

    assert "bind" not in private, "в личке привязывать нечего"
    assert "task" not in private, "создание задачи требует контекста чата"
    assert "mychats" not in groups, "список своих чатов — личное дело"
    # Админ группы видит всё, что видит участник, плюс своё.
    assert groups < admins
    assert {"bind", "unbind"} <= admins


def test_admin_commands_are_hidden_from_ordinary_members() -> None:
    """Показать команду и отказать по правам — хуже, чем не показывать."""
    groups = {c["command"] for c in commands.for_scope(commands.GROUPS)}
    assert "bind" not in groups
    assert "unbind" not in groups


@pytest.mark.parametrize("scope", commands.scopes())
def test_telegram_limits_are_respected(scope: str) -> None:
    """Требования Telegram к setMyCommands: до 100 команд, имя [a-z0-9_] 1–32,
    описание 1–256. Нарушение — отказ на всё меню целиком, а не на одну строку."""
    items = commands.for_scope(scope)
    assert 0 < len(items) <= 100
    for item in items:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", item["command"]), item
        assert 1 <= len(item["description"]) <= 256, item


def test_no_duplicate_commands_in_one_scope() -> None:
    for scope in commands.scopes():
        names = [c["command"] for c in commands.for_scope(scope)]
        assert len(names) == len(set(names)), scope


def test_help_covers_every_command_of_the_scope() -> None:
    """`/help` и меню строятся из одного списка — расходиться им негде."""
    for private in (True, False):
        text = commands.render_help(private=private)
        scope = commands.PRIVATE if private else commands.GROUPS
        expected = {c["command"] for c in commands.for_scope(scope)}
        if not private:
            expected |= {c["command"] for c in commands.for_scope(commands.ADMINS)}
        for name in expected:
            assert f"/{name}" in text, f"{name} нет в /help (private={private})"


def test_help_escapes_argument_placeholders() -> None:
    """`<номер>` в parse_mode=HTML Telegram принимает за тег и режет строку (И-6)."""
    text = commands.render_help(private=False)
    assert "&lt;номер&gt;" in text
    assert "<номер>" not in text


def test_task_number_shortcut_is_documented() -> None:
    """`/t_215` — не команда из меню, но узнать о нём человек может только из /help."""
    assert "/t_" in commands.render_help(private=False)


# ------------------------------------------------- кнопки постоянной клавиатуры
# Обещание то же, что у меню команд, только нарушается тише: нажатие постоянной
# кнопки приходит обычным ТЕКСТОМ, без `callback_query`. Подпись, которой нет
# в `PRIVATE_LABELS`, проваливается до общего ответа с подсказкой — и снаружи это
# выглядит как «бот не реагирует на кнопки». Так уже было с «📊 Мои задачи».
def test_every_private_button_is_understood() -> None:
    from b24bot.bot import keyboards

    labels = {b["text"] for row in keyboards.persistent_private()["keyboard"]
              for b in row if "web_app" not in b}
    orphans = labels - set(keyboards.PRIVATE_LABELS)
    assert not orphans, f"кнопка нарисована, но не разбирается: {orphans}"


def test_every_private_action_is_handled() -> None:
    """И обратная сторона: разобранная подпись обязана доехать до ветки действия."""
    body = HANDLERS[HANDLERS.index("async def _private_action("):]
    body = body[:body.index("\n# ")]
    from b24bot.bot import keyboards

    for action in set(keyboards.PRIVATE_LABELS.values()):
        assert re.search(rf'"{action}"', body), f"{action} не разбирается в _private_action"
