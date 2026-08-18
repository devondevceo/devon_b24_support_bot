"""Реестр слеш-команд бота.

Один список на всё: из него строится меню Telegram (`setMyCommands`), текст `/help`
и он же служит проверкой, что команда, которую видит человек, действительно
обрабатывается роутером — тест сверяет реестр с `handlers`.

Меню разное в личке, в группе и у администраторов группы, потому что команды разные.
Показывать в личке `/bind` бессмысленно: привязывать нечего. Показывать всем в группе
`/unbind` — значит звать нажать и получить отказ.

Область `all_chat_administrators` — это администраторы чата **Telegram**, а не админы
теннанта. Совпадение неточное, поэтому меню только прячет команду от лишних глаз;
настоящую проверку роли делает `access.is_tenant_admin` в момент действия.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from b24bot.core.text import esc_html

PRIVATE = "all_private_chats"
GROUPS = "all_group_chats"
ADMINS = "all_chat_administrators"


@dataclass(frozen=True)
class Command:
    name: str
    description: str          # до 256 символов, показывается в меню Telegram
    scopes: tuple[str, ...]
    help_group: str           # раздел в /help
    args: str = ""            # подсказка в тексте /help

    @property
    def title(self) -> str:
        """Подпись для /help. Аргументы экранируются: `<номер>` в parse_mode=HTML
        Telegram принимает за тег и режет строку целиком (И-6)."""
        args = esc_html(self.args)
        return f"/{self.name}{(' ' + args) if args else ''}"


# Порядок внутри группы — порядок в меню Telegram и в /help.
COMMANDS: tuple[Command, ...] = (
    # ---------------------------------------------------------------- везде
    Command("help", "что умеет бот и список команд", (PRIVATE, GROUPS, ADMINS),
            "Общее"),
    Command("start", "начать работу с ботом", (PRIVATE,), "Общее"),
    Command("whoami", "кто я в системе: теннант, Битрикс24, привязка",
            (PRIVATE, GROUPS, ADMINS), "Общее"),
    Command("link", "привязать аккаунт Telegram к Битрикс24",
            (PRIVATE, GROUPS, ADMINS), "Общее"),

    # ---------------------------------------------------------------- задачи
    Command("task", "создать задачу: реплаем, с текстом или по вопросам",
            (GROUPS, ADMINS), "Задачи", args="[текст]"),
    Command("ask", "создать задачу по вопросам", (GROUPS, ADMINS), "Задачи"),
    Command("status", "сводка по проектам чата", (PRIVATE, GROUPS, ADMINS), "Задачи"),
    Command("list", "все открытые задачи", (PRIVATE, GROUPS, ADMINS), "Задачи"),
    Command("overdue", "просроченные задачи", (PRIVATE, GROUPS, ADMINS), "Задачи"),
    Command("comment", "комментарий в задачу: текстом или ответом на сообщение",
            (GROUPS, ADMINS), "Задачи", args="<номер> [текст]"),
    Command("discussion", "показать обсуждение задачи", (GROUPS, ADMINS), "Задачи",
            args="<номер>"),
    Command("cancel", "прервать начатый опрос", (GROUPS, ADMINS), "Задачи"),

    # ---------------------------------------------------------------- личка
    Command("mychats", "мои чаты и проекты", (PRIVATE,), "В личке"),
    Command("pending", "задачи, ожидающие моего подтверждения", (PRIVATE,), "В личке"),

    # ------------------------------------------------------- админ теннанта
    Command("bind", "привязать чат к проекту Битрикс24", (ADMINS,),
            "Администратору теннанта"),
    Command("bindings", "какие проекты привязаны к чату", (GROUPS, ADMINS),
            "Администратору теннанта"),
    Command("unbind", "отвязать проект от чата", (ADMINS,),
            "Администратору теннанта"),
)

# Не команда, а формат: номер задачи подставляется в имя. В меню Telegram такое
# не покажешь, но в /help про него написать обязательно — иначе о нём не узнают.
SPECIAL_HELP: tuple[tuple[str, str], ...] = (
    ("/t_<номер>", "открыть карточку задачи, например /t_215"),
)

NAMES = frozenset(c.name for c in COMMANDS)


def for_scope(scope: str) -> list[dict[str, str]]:
    """Список для `setMyCommands`. Telegram требует именно `command` и `description`."""
    return [{"command": c.name, "description": c.description[:256]}
            for c in COMMANDS if scope in c.scopes]


def scopes() -> tuple[str, ...]:
    return (PRIVATE, GROUPS, ADMINS)


@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)


def help_sections(*, private: bool) -> list[Section]:
    """Разделы для текста `/help`. В личке и в группе они разные."""
    scope = PRIVATE if private else GROUPS
    admin_scope = None if private else ADMINS

    out: list[Section] = []
    for cmd in COMMANDS:
        if scope not in cmd.scopes and (admin_scope is None
                                        or admin_scope not in cmd.scopes):
            continue
        section = next((s for s in out if s.title == cmd.help_group), None)
        if section is None:
            section = Section(cmd.help_group)
            out.append(section)
        section.lines.append(f"• <code>{cmd.title}</code> — {cmd.description}")

    if not private:
        tasks = next((s for s in out if s.title == "Задачи"), None)
        if tasks is not None:
            tasks.lines += [f"• <code>{esc_html(name)}</code> — {text}"
                            for name, text in SPECIAL_HELP]
    return out


def render_help(*, private: bool) -> str:
    parts = []
    for section in help_sections(private=private):
        parts.append(f"<b>{section.title}</b>\n" + "\n".join(section.lines))
    return "\n\n".join(parts)
