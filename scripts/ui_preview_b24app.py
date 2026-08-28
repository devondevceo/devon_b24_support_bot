"""Превью экранов приложения Б24 без базы и портала — для аудита вёрстки.

Собирает вкладку «Чаты» (все состояния: работает, бот удалён, без проекта,
несколько проектов, вид не-администратора) и раздел «Подтверждение» из тех же
компонентов и тех же функций, что боевой рендер. Итог — самодостаточный HTML,
открывается file://, сервера не требует (петлевые соединения в среде
разработки закрыты).

Запуск:  PYTHONPATH=src python scripts/ui_preview_b24app.py [--out путь.html]
Обычно вызывается не руками, а раннером `node scripts/audit_b24app.mjs`.
"""
from __future__ import annotations

import argparse
import pathlib
import tempfile

from b24bot.api import app_approval, app_ui
from b24bot.api import ui_kit as ui
from b24bot.domain import approvals, sync

SESSION = "preview-session"


def _chat(cid: int, chat_id: int, title: str, status: str = "active",
          forum: bool = False) -> dict[str, object]:
    return {"id": cid, "chat_id": chat_id, "title": title, "status": status,
            "is_forum": forum}


def _binding(bid: int, project: str, client: str, group: int) -> dict[str, object]:
    return {"id": bid, "chat_ref": 0, "project_id": bid, "project": project,
            "b24_group_id": group, "client_id": 1, "client": client}


_PORTAL = [
    {"id": 11, "name": "Devon SD BOT", "role": "A", "extranet": False},
    {"id": 12, "name": "Линия Жизни Битрикс24", "role": "A", "extranet": False},
    {"id": 13, "name": "Линия Жизни · сайт", "role": "E", "extranet": False},
    {"id": 14, "name": "Внутренние задачи DEVON", "role": "A", "extranet": False},
]
_CLIENTS = [{"id": 1, "name": "Devon SD BOT"}, {"id": 2, "name": "Линия Жизни"}]

# Названия и состояния повторяют скриншот с прода 29.08.2026 плюс состояния,
# которых на нём не было: чат с двумя проектами и чат без единой привязки.
_CHATS: list[tuple[dict[str, object], list[dict[str, object]]]] = [
    (_chat(1, -5075800320, "Devon Test Support"),
     [_binding(1, "Devon SD BOT", "Devon SD BOT", 11)]),
    (_chat(2, -5514694473, "Devon Test Support 2", status="left"), []),
    (_chat(3, -1004354129141, "Линия Жизни х DevON / Поддержка Б24", forum=True),
     [_binding(2, "Линия Жизни Битрикс24", "Линия Жизни", 12),
      _binding(3, "Линия Жизни · сайт", "Линия Жизни", 13)]),
    (_chat(4, -5222333444, "ЛЖ · новый чат внедрения", status="unclaimed"), []),
]


def build() -> str:
    def sections(is_admin: bool, chats: list[tuple[dict[str, object],
                                                   list[dict[str, object]]]]) -> str:
        return "".join(
            app_ui._chat_section(ch, linked, _PORTAL, {11, 12}, _CLIENTS,
                                 SESSION, "chats", is_admin)
            for ch, linked in chats)

    chats_admin = ui.panel(
        "Чаты и проекты", sections(True, _CHATS), icon_name="chat", flush=True,
        actions_html='<span class="panel-note tnum">чатов: 4 · привязок: 3</span>',
        footer_html=ui.hint("Новый чат появляется здесь сам, как только "
                            "Telegram-бота добавят в группу."))
    chats_ro = ui.panel(
        "Чаты и проекты — вид сотрудника", sections(False, _CHATS[:3]),
        icon_name="chat", flush=True,
        footer_html=ui.hint("Управлять привязками может администратор портала."))

    stages = [sync.Stage(333, "Новые", 100, None, None),
              sync.Stage(335, "Выполняются", 200, None, None),
              sync.Stage(337, "Сделаны", 300, None, None)]
    settings = approvals.Settings(
        project_id=7, enabled=True, responsible_user_id=3,
        confirm_stage_id=335, confirm_stage_title="Выполняются",
        reject_stage_id=333, reject_stage_title="Новые")
    member = {"user_id": 3, "display_name": "Мария Орлова", "tg_username": "m_orlova"}
    approval_panel = ui.panel(
        "Подтверждение задач",
        app_approval._project_row(
            SESSION, "approval", 7, "Линия Жизни Битрикс24", "Линия Жизни",
            settings, [member], stages, True)
        + app_approval._project_row(
            SESSION, "approval", 8, "Devon SD BOT", "Devon SD BOT",
            None, [], stages, False),
        icon_name="check-circle", flush=True)

    tabs = app_ui._tabs_html("chats", [
        ("overview", "Обзор", "info", 0),
        ("chats", "Чаты", "chat", 4),
        ("bot", "Бот", "send", 0),
        ("survey", "Опросник", "inbox", 0),
        ("approval", "Подтверждение", "check-circle", 0),
        ("notify", "Уведомления", "alert", 0),
        ("team", "Команда", "users", 5),
    ])
    head = app_ui._head_html(
        "Поддержка в Telegram",
        f'{ui.icon("shield", 13)}<span>devondev.bitrix24.ru</span>'
        f"<span>·</span><span>администратор портала</span>")

    body = (head + tabs
            + app_ui._panel_html(
                "chats", "chats",
                chats_admin + '<div class="divider"></div>' + chats_ro
                + '<div class="divider"></div>' + approval_panel))

    html = app_ui.page(body, None).body.decode("utf-8")
    # BX24-скрипт с file:// не грузится и превью не нужен.
    return html.replace('<script src="//api.bitrix24.com/api/v1/"></script>', "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default = pathlib.Path(tempfile.gettempdir()) / "b24app_preview.html"
    parser.add_argument("--out", type=pathlib.Path, default=default)
    args = parser.parse_args()
    args.out.write_text(build(), encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
