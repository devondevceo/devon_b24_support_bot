"""Отрисовка экранов приложения в Битрикс24: И-6 и поведение вкладок.

Разметка теперь собирается компонентами `ui_kit`, а не f-строками по месту.
Это удобнее, но и опаснее: один компонент, забывший экранировать, протекает
сразу на все экраны. Поэтому каждый компонент проверяется на враждебном вводе.

Источники враждебных строк реальны и все до одного не наши:
  * название чата Telegram задаёт тот, кто создал чат;
  * название проекта и имя сотрудника приходят из Битрикса;
  * имя в Telegram человек ставит себе сам.
"""
from __future__ import annotations

import re

import pytest

from b24bot.api import app_ui
from b24bot.api import ui_kit as ui
from b24bot.core.text import esc_html

# Полный набор: закрытие тега, закрытие атрибута в обеих кавычках, обработчик
# события, сущность, скобки BBCode.
EVIL = "</span><script>alert(1)</script><img src=x onerror=alert(2)>\" ' & [b]"

# Теги, которые компоненты создают сами. Всё, что сверх этого, — инъекция.
_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)")


def tags(html: str) -> list[str]:
    return _TAG_RE.findall(html)


# Проверять подстроку «onerror» бессмысленно: экранированный текст
# `&lt;img src=x onerror=...&gt;` — это инертная строка на экране, и слово в ней
# остаётся. Значение имеет только то, стал ли ввод РАЗМЕТКОЙ, поэтому разбор
# идёт по тегам, а содержимое кавычек пропускается целиком: внутри значения
# атрибута `onerror=` — такие же данные, как и любые другие.
_TAG_RE_FULL = re.compile(r"<([a-zA-Z][^>]*)>")
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*)""")


def attr_names(html: str) -> list[str]:
    """Имена атрибутов всех настоящих тегов разметки."""
    out: list[str] = []
    for body in _TAG_RE_FULL.findall(html):
        out += [n.lower() for n in _ATTR_RE.findall(body)]
    return out


def assert_no_injection(html: str) -> None:
    """Враждебная строка не превратилась ни в тег, ни в атрибут."""
    lowered = html.lower()
    # Экранирование превращает `<script` в `&lt;script`, поэтому настоящий тег
    # из полезной нагрузки виден сразу.
    for tag in ("<script", "<img", "<iframe", "<object", "<style"):
        assert tag not in lowered, f"полезная нагрузка стала тегом {tag}"
    # Обработчик события — это ИМЯ атрибута, а не текст внутри значения.
    handlers = [n for n in attr_names(html) if n.startswith("on")]
    assert not handlers, f"в разметке появились обработчики: {handlers}"
    # Выход из значения атрибута: `<` внутри значения означает закрытую кавычку.
    for value in re.findall(r'="([^"]*)"', html):
        assert "<" not in value, f"выход из атрибута: {value[:60]}"
    for value in re.findall(r"='([^']*)'", html):
        assert "<" not in value, f"выход из атрибута: {value[:60]}"


# ------------------------------------------------------------------ компоненты
@pytest.mark.parametrize("render", [
    lambda: ui.badge(EVIL, "ok"),
    lambda: ui.field(EVIL, "значение"),
    lambda: ui.stat(EVIL, EVIL),
    lambda: ui.empty(EVIL, EVIL),
    lambda: ui.hint(EVIL),
    lambda: ui.step(1, EVIL, EVIL, state="now"),
    lambda: ui.panel(EVIL, "тело"),
    lambda: ui.link_button("https://t.me/bot", EVIL),
    lambda: ui.goto_button("chats", EVIL),
])
def test_components_escape_hostile_text(render: object) -> None:
    assert_no_injection(render())  # type: ignore[operator]


def test_html_suffix_marks_the_boundary_of_responsibility() -> None:
    """Соглашение модуля: `*_html` — уже готовая разметка, остальное экранируется.

    Проверяется именно граница: `item()` берёт HTML и обязан пропустить теги
    вызывающего, а с экранированным вводом обязан быть чистым. Если однажды
    кто-то поменяет это в одну сторону, тест упадёт в другую.
    """
    assert "<b>жирный</b>" in ui.item("<b>жирный</b>")
    assert_no_injection(ui.item(esc_html(EVIL), sub_html=esc_html(EVIL)))
    assert_no_injection(ui.row(esc_html(EVIL), sub_html=esc_html(EVIL)))


def test_action_form_escapes_label_and_fields() -> None:
    html = ui.action_form("/b24/app/chat", {"session": EVIL, "binding_id": 7},
                          EVIL, confirm=EVIL)
    assert_no_injection(html)
    # Значение скрытого поля не должно выходить за пределы атрибута.
    assert 'value="</span>' not in html


def test_action_form_disabled_button_has_reason() -> None:
    """Выключенная кнопка без объяснения выглядит как поломка интерфейса."""
    html = ui.action_form("/x", {}, "Снять права", disabled=True,
                          title="Это единственный админ теннанта")
    assert "disabled" in html
    assert "единственный админ" in html


def test_icon_unknown_name_is_empty_not_broken_markup() -> None:
    assert ui.icon("нет-такой-иконки") == ""
    assert ui.icon("send").startswith("<svg")


def test_icon_paths_are_all_wellformed() -> None:
    """Битый path рисует случайную кляксу поверх интерфейса, и это молча."""
    for name in ui._PATHS:
        svg = ui.icon(name)
        assert svg.count("<path") >= 1
        assert 'd="M' in svg or 'd="m' in svg


# ---------------------------------------------------------------------- формы
def _chat(cid: int = 1) -> dict[str, object]:
    return {"id": cid, "chat_id": -100123, "title": EVIL, "status": "active",
            "is_forum": False}


def test_bind_form_escapes_portal_project_names() -> None:
    """Название группы приходит с портала: переименовать её может сотрудник."""
    html = app_ui._bind_form(
        _chat(), [],
        [{"id": 12, "name": EVIL, "role": "A", "extranet": False}],
        set(), [{"id": 3, "name": EVIL}], "sess", "chats")
    assert_no_injection(html)
    assert "&lt;script&gt;" in html


def test_bind_form_carries_active_tab() -> None:
    """Без этого поля любое действие возвращает человека на «Обзор»."""
    html = app_ui._bind_form(
        _chat(), [], [{"id": 12, "name": "Проект", "role": "A", "extranet": False}],
        set(), [], "sess", "chats")
    assert '<input type="hidden" name="tab" value="chats">' in html


def test_bind_form_reports_when_nothing_left_to_bind() -> None:
    """Пустая форма без объяснения читается как сломанная."""
    html = app_ui._bind_form(
        _chat(), [{"b24_group_id": 12}],
        [{"id": 12, "name": "Проект", "role": "A", "extranet": False}],
        set(), [], "sess", "chats")
    assert "уже привязаны" in html
    assert "<select" not in html


def test_bind_form_expanded_only_for_unbound_chat() -> None:
    """У чата без привязок форма раскрыта: привязка и есть следующий шаг.
    У чата с привязками — свёрнута, иначе вкладка превращается в простыню."""
    projects = [{"id": 12, "name": "Проект", "role": "A", "extranet": False},
                {"id": 15, "name": "Другой", "role": "A", "extranet": False}]
    unbound = app_ui._bind_form(_chat(), [], projects, set(), [], "sess", "chats")
    assert '<details class="bind" open>' in unbound
    bound = app_ui._bind_form(
        _chat(), [{"b24_group_id": 12, "client_id": 3, "client": "Линия Жизни"}],
        projects, set(), [], "sess", "chats")
    assert '<details class="bind">' in bound


def test_bind_form_locks_client_when_chat_already_has_one() -> None:
    """Один чат — один клиент: селект предлагал бы выбор между «правильно»
    и «ошибка сервера». После первой привязки клиент фиксирован."""
    html = app_ui._bind_form(
        _chat(), [{"b24_group_id": 12, "client_id": 3, "client": "Линия Жизни"}],
        [{"id": 12, "name": "Проект", "role": "A", "extranet": False},
         {"id": 15, "name": "Другой", "role": "A", "extranet": False}],
        set(), [{"id": 3, "name": "Линия Жизни"}, {"id": 4, "name": "Ромашка"}],
        "sess", "chats")
    assert 'name="client_id" value="3"' in html      # клиент едет скрытым полем
    assert html.count("<select") == 1                # выбирается только проект
    assert "Линия Жизни" in html                     # и назван человеку текстом
    assert "Ромашка" not in html                     # чужие клиенты не предлагаются


def test_project_row_title_and_client_are_separate_blocks() -> None:
    """Спаны здесь однажды склеились в «Devon SD BOTклиент Devon SD BOT»."""
    html = app_ui._project_row(EVIL, EVIL)
    assert '<div class="proj-t">' in html
    assert '<div class="proj-s">' in html
    assert_no_injection(html)


# --------------------------------------------------------------------- вкладки
@pytest.mark.parametrize("raw", ["overview", "chats", "bot", "survey", "team"])
def test_safe_tab_allows_known(raw: str) -> None:
    assert app_ui.safe_tab(raw) == raw


def test_every_tab_has_a_panel() -> None:
    """Вкладка без панели — кнопка, которая ничего не открывает."""
    for key in app_ui.TABS:
        assert f'id="panel-{key}"' in app_ui._panel_html(key, key, "тело")


@pytest.mark.parametrize("raw", ["", "../etc", "<script>", "OVERVIEW", "team ", "9"])
def test_safe_tab_rejects_everything_else(raw: str) -> None:
    """Значение приходит из формы, то есть от клиента, и в разметку идёт как id."""
    assert app_ui.safe_tab(raw) == "overview"


def test_tabs_count_is_not_glued_into_accessible_name() -> None:
    """Иначе скринридер произносит «Чаты3»."""
    html = app_ui._tabs_html("chats", [("chats", "Чаты", "chat", 3)])
    assert 'aria-label="Чаты, 3"' in html
    assert 'aria-hidden="true"' in html


def test_tabs_mark_exactly_one_selected_and_roving_tabindex() -> None:
    html = app_ui._tabs_html("bot", [("overview", "Обзор", "info", 0),
                                     ("chats", "Чаты", "chat", 2),
                                     ("bot", "Бот", "send", 0)])
    assert html.count('aria-selected="true"') == 1
    assert html.count('tabindex="0"') == 1
    assert html.count('tabindex="-1"') == 2


def test_panel_hidden_for_inactive_tabs() -> None:
    assert " hidden>" in app_ui._panel_html("chats", "bot", "тело")
    assert " hidden>" not in app_ui._panel_html("bot", "bot", "тело")


# --------------------------------------------------------- состояние интеграции
def _bot(**over: object) -> dict[str, object]:
    base: dict[str, object] = {"status": "active", "privacy_mode_off": True,
                               "last_error": None, "username": "b", "mode": "polling"}
    base.update(over)
    return base


def test_health_without_bot_is_the_first_thing_to_fix() -> None:
    kind, title, _ = app_ui._health(None, 0, 0)
    assert kind == "warn"
    assert "не подключён" in title


def test_health_privacy_mode_on_is_an_error_not_a_note() -> None:
    """Бот формально «работает», но продукта при этом нет."""
    kind, title, _ = app_ui._health(_bot(privacy_mode_off=False), 5, 5)
    assert kind == "err"
    assert "privacy mode" in title.lower()


def test_health_suspended_outranks_privacy() -> None:
    """Порядок проверок — это порядок, в котором всё ломается."""
    kind, title, _ = app_ui._health(
        _bot(status="suspended", privacy_mode_off=False, last_error="токен утёк"), 5, 5)
    assert kind == "err"
    assert "приостановлен" in title


def test_health_no_bindings_is_warning_not_success() -> None:
    kind, _, _ = app_ui._health(_bot(), 3, 0)
    assert kind == "warn"


def test_health_all_good() -> None:
    kind, title, _ = app_ui._health(_bot(), 3, 2)
    assert kind == "ok"
    assert "работает" in title


def test_health_error_text_from_telegram_is_escaped_by_caller() -> None:
    """_health отдаёт СЫРОЙ текст: экранирует тот, кто ставит его в разметку."""
    _, _, detail = app_ui._health(_bot(status="error", last_error=EVIL), 1, 1)
    assert detail == EVIL
    assert_no_injection(ui.banner(esc_html(detail), "err"))
