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


# ---------------------------------------------------------------- /t_<номер>
# Обещан в /help с первого дня и в сводке уведомлений без кнопок, а разобран не
# был ни разу: имя `t_215` не совпадало ни с одной веткой роутера, и бот молчал.
# Страж `test_every_advertised_command_is_handled` этого не видел — форма стоит
# не в реестре команд, а в `SPECIAL_HELP`, поэтому и проверка у неё своя.
def test_card_shortcut_parses_only_its_own_form() -> None:
    from b24bot.bot import handlers

    assert handlers.card_shortcut("t_215") == 215
    for name in ("t_", "t_0", "t_12a", "t", "time", "task", "t_215_1", "tt_215"):
        assert handlers.card_shortcut(name) is None, name


def test_every_special_help_form_is_routed() -> None:
    """То, что /help называет формой команды, роутер обязан узнавать."""
    from b24bot.bot import handlers

    for form, _text in commands.SPECIAL_HELP:
        example = re.search(r"/(t_\d+)", _text)
        assert form.startswith("/t_") and example, form
        assert handlers.card_shortcut(example.group(1)) is not None, form


def test_card_shortcut_opens_the_card_through_the_one_door(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Та же дверь, что у кнопки с номером: `_open_card` с проверкой И-3 внутри."""
    import asyncio

    from b24bot.bot import handlers
    from b24bot.domain import context

    seen: list[tuple[int, int, int]] = []

    async def open_card(ctx: context.ChatContext, tg_user_id: int,
                        task_id: int) -> handlers.Reply:
        seen.append((ctx.chat_ref, tg_user_id, task_id))
        return handlers.Reply("карточка")

    monkeypatch.setattr(handlers, "_open_card", open_card)
    ctx = context.ChatContext(
        chat_ref=5, chat_id=-100500, title="Поддержка", status="active",
        tenant_id=1, is_forum=False,
        projects=[context.ProjectRef(11, 101, "Devon SD BOT", "Линия Жизни")])

    # Имя разбирается тем же `_command`, что и живое сообщение, — вместе с
    # упоминанием бота, которое Telegram дописывает в группе с несколькими ботами.
    cmd = handlers._command("/t_215@devon_sd_bot", "devon_sd_bot")
    assert cmd == ("t_215", "")
    reply = asyncio.run(handlers._group_command(
        {"username": "devon_sd_bot"}, ctx, cmd, {"chat": {"id": -100500}}, 77, {}))
    assert seen == [(5, 77, 215)]
    assert reply is not None and reply.text == "карточка"


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


# --------------------------------------------------- мини-апп из лички
APP = "https://example.test/miniapp"


def test_private_keyboard_never_carries_a_web_app_button() -> None:
    """Кнопка `web_app` на постоянной клавиатуре открывает мини-апп без подписи.

    WebAppInitData «is empty if the Mini App was launched from a keyboard button»
    (core.telegram.org/bots/webapps): приложение не знает, кто перед ним, и
    отвечает «работает только внутри Telegram» — внутри Telegram. Так кнопка
    «🧩 Приложение» и жила с 16.08 до 14.09.2026. Мини-апп открывают инлайн-кнопка
    и кнопка меню бота; на клавиатуре — только текст.
    """
    from b24bot.bot import keyboards

    rows = keyboards.persistent_private(APP)["keyboard"]
    assert not [b for row in rows for b in row if "web_app" in b]
    assert "🧩 Приложение" in {b["text"] for row in rows for b in row}
    # Мини-апп не развёрнут — звать в него нечем и незачем.
    assert "🧩 Приложение" not in {
        b["text"] for row in keyboards.persistent_private(None)["keyboard"] for b in row}


def test_app_button_answers_with_signed_inline_buttons(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответ на «🧩 Приложение» — инлайн-кнопки `web_app`: у них подпись есть.

    Вторая кнопка открывает приложение сразу на инструкции — это вход в неё
    из бота, привязка для него не нужна.
    """
    import asyncio

    from b24bot.bot import handlers

    monkeypatch.setattr(handlers.miniapp, "web_app_url", lambda: APP)
    reply = asyncio.run(handlers._private_action("app", 77))
    assert reply.markup is not None
    buttons = [b for row in reply.markup["inline_keyboard"] for b in row]
    assert [b["web_app"]["url"] for b in buttons] == [APP, f"{APP}?open=guide"]
    assert [b["text"] for b in buttons] == ["🧩 Открыть приложение", "📖 Инструкция"]


def test_app_button_without_a_deployed_app_says_so(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from b24bot.bot import handlers, texts

    monkeypatch.setattr(handlers.miniapp, "web_app_url", lambda: None)
    reply = asyncio.run(handlers._private_action("app", 77))
    assert reply.text == texts.MSG_APP_UNAVAILABLE


def test_private_help_points_to_the_guide(monkeypatch: pytest.MonkeyPatch) -> None:
    """/help — какие есть команды; как ими пользоваться — инструкция в приложении."""
    from b24bot.bot import handlers

    monkeypatch.setattr(handlers.miniapp, "web_app_url", lambda: APP)
    assert "📖 Инструкция" in handlers._help_text(private=True)
    # Нет приложения — нет и строки, зовущей в него.
    monkeypatch.setattr(handlers.miniapp, "web_app_url", lambda: None)
    assert "📖 Инструкция" not in handlers._help_text(private=True)
