"""Номер задачи ссылкой на портал.

Номер задачи — главный переход из чата наружу, и до сих пор он был просто
текстом: чтобы открыть задачу, человек копировал число и искал его на портале.
Здесь проверяется, что ссылка появляется там, где известен портал, и что без
портала номер остаётся номером, а не исчезает вместе со ссылкой.
"""
from __future__ import annotations

from b24bot.bot.views import portal_task_url, render_card, render_list, task_ref
from b24bot.domain.context import ProjectRef

PROJECT = ProjectRef(1, 6, "Devon SD BOT", "Линия Жизни")
TASK = {"id": 233, "title": "Добавить новую лид-форму", "status": 3, "groupId": 6,
        "responsible": {"name": "Роман Давыдов"}, "creator": {"name": "Сергей Крищунс"},
        "createdDate": "2026-08-18T00:34:00+03:00"}


def test_number_becomes_a_link() -> None:
    html = task_ref(233, domain="devondev.bitrix24.ru", b24_user_id=7)
    assert html == ('<a href="https://devondev.bitrix24.ru/company/personal/user/7'
                    '/tasks/task/view/233/">#233</a>')


def test_url_matches_the_form_used_by_the_card_button() -> None:
    """Ссылка в тексте и кнопка «Открыть в Б24» обязаны вести в одно место."""
    assert portal_task_url("p.example", 233, 7) in task_ref(
        233, domain="p.example", b24_user_id=7)


def test_number_survives_without_a_portal() -> None:
    """Портал неизвестен — ссылки не выйдет, но номер пропасть не имеет права:
    по нему открывают карточку и его набирают в /comment."""
    assert task_ref(233) == "#233"
    assert task_ref(233, domain="", b24_user_id=7) == "#233"
    assert task_ref(233, domain="p.example", b24_user_id=None) == "#233"
    assert task_ref(233, domain="p.example", b24_user_id=0) == "#233"


def test_garbage_does_not_produce_a_broken_link() -> None:
    assert task_ref(None, domain="p.example", b24_user_id=7) == "#None"
    assert task_ref("", domain="p.example", b24_user_id="абв") == "#"


def test_card_and_list_carry_the_link() -> None:
    card = render_card(TASK, PROJECT, domain="p.example", b24_user_id=7)
    assert '<a href="https://p.example/company/personal/user/7/tasks/task/view/233/">' \
        in card
    listing = render_list([TASK], title="Все задачи", domain="p.example", b24_user_id=7)
    assert "/tasks/task/view/233/" in listing


def test_card_and_list_work_without_the_portal() -> None:
    """Ни один экран не обязан падать из-за неизвестного домена."""
    assert "#233" in render_card(TASK, PROJECT)
    assert "#233" in render_list([TASK], title="Все задачи")
