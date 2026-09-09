"""Паритет мини-аппа с ботом: сводка, трудозатраты, опросник, файлы, «кто я».

Проверяется то, что ломается тихо и правдоподобно: сводка, где два разных смысла
слиты в одну строку; свод трудозатрат, разрезы которого не сходятся с итогом;
опросник, потерявший обязательный ответ; файл, уехавший в задачу дважды.

Стенд общий с `test_miniapp_api`: там же живут подпись, поддельный портал и пустая
база. Своя копия фикстур означала бы два места, где чинить одно и то же.
"""
from __future__ import annotations

from typing import Any

import pytest

from b24bot.api import miniapp as api_miniapp
from b24bot.bot import survey, views
from b24bot.domain import timesheet
from tests.test_miniapp_api import (
    CHAT_REF,
    GROUP_ID,
    FakeClient,
    init_data,
    portal,  # noqa: F401  — фикстура подключается по имени
    request,
    task_body,
)

BASE = "/api/miniapp"


def _stages(*_: object, **__: object) -> Any:
    async def call() -> list[tuple[int, str]]:
        return [(333, "Новые"), (335, "Выполняются"), (337, "Сделаны")]

    return call()


# ------------------------------------------------------------------- сводка
async def test_summary_counts_tasks_by_stage(portal: FakeClient,  # noqa: F811
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "stages_of", _stages)
    portal.handler = lambda method, params: (
        {"tasks": [task_body(1, stageId="333"), task_body(2, stageId="335"),
                   task_body(3, stageId="335")]}
        if method == "tasks.task.list" else {"ok": True})

    resp = await request("GET", f"{BASE}/summary?chat_ref={CHAT_REF}", auth=init_data())
    assert resp.status_code == 200
    body = resp.json()
    assert body["open"] == 3
    rows = {r["title"]: r["count"] for r in body["projects"][0]["stages"]}
    assert rows == {"Новые": 1, "Выполняются": 2}


async def test_summary_keeps_outside_and_unknown_apart(
        portal: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Два РАЗНЫХ смысла и две разные строки.

    `stageId=0` — задача не разложена по колонкам, это нормальное состояние.
    Стадия, которой нет в справочнике, — уже наш разлад с порталом. Слить их
    в одну строку значит спрятать второй смысл навсегда.
    """
    monkeypatch.setattr(views, "stages_of", _stages)

    async def not_fresh(*_: object, **__: object) -> bool:
        return False

    monkeypatch.setattr(api_miniapp.sync, "ensure_fresh", not_fresh)
    portal.handler = lambda method, params: (
        {"tasks": [task_body(1, stageId="0"), task_body(2, stageId="99999")]}
        if method == "tasks.task.list" else {"ok": True})

    body = (await request("GET", f"{BASE}/summary?chat_ref={CHAT_REF}",
                          auth=init_data())).json()
    project = body["projects"][0]
    assert project["outside"] == 1
    assert project["unresolved"] == 1
    assert project["outside_title"] != project["unresolved_title"]


async def test_summary_is_bounded_by_group_ids(portal: FakeClient,  # noqa: F811
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Граница выборки — только `GROUP_ID`, как и у всех остальных путей (И-3)."""
    monkeypatch.setattr(views, "stages_of", _stages)
    await request("GET", f"{BASE}/summary?chat_ref={CHAT_REF}", auth=init_data())
    params = portal.params_of("tasks.task.list")
    assert params["filter"]["GROUP_ID"] == [GROUP_ID]


# ------------------------------------------------------------ трудозатраты
async def test_timesheet_offers_months(portal: FakeClient) -> None:  # noqa: F811
    body = (await request("GET", f"{BASE}/timesheet/months", auth=init_data())).json()
    assert len(body["items"]) == timesheet.MONTHS_OFFERED
    assert body["current"] == body["items"][0]["value"]


async def test_timesheet_breakdowns_match_the_total(
        portal: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Оба разреза обязаны сойтись с итогом.

    Расхождение здесь означало бы ошибку в подсчёте, и увидеть его иначе, чем
    сложив столбцы, нельзя: числа в отчёте выглядят одинаково уверенно.
    """
    monkeypatch.setattr(views, "stages_of", _stages)

    snap = timesheet.Snapshot(
        tasks={
            1: timesheet.TaskRef(1, "Первая", 3, 335),
            2: timesheet.TaskRef(2, "Вторая", 5, 337),
        },
        entries=[
            timesheet.Entry(1, 1, 3600, _at(2026, 8, 3)),
            timesheet.Entry(2, 1, 1800, _at(2026, 8, 4)),
            timesheet.Entry(3, 2, 7200, _at(2026, 8, 5)),
            # Запись прошлого месяца в свод августа попасть не должна.
            timesheet.Entry(4, 2, 9000, _at(2026, 7, 30)),
        ],
        complete=True, seen=4, total_on_portal=4, at=_at(2026, 8, 20))

    async def fake_snapshot(*_: object, **__: object) -> timesheet.Snapshot:
        return snap

    monkeypatch.setattr(timesheet, "snapshot", fake_snapshot)

    body = (await request("GET", f"{BASE}/timesheet?chat_ref={CHAT_REF}&month=2026-08",
                          auth=init_data())).json()
    assert body["total_seconds"] == 3600 + 1800 + 7200
    assert sum(b["seconds"] for b in body["by_status"]) == body["total_seconds"]
    assert sum(b["seconds"] for b in body["by_stage"]) == body["total_seconds"]
    assert body["task_count"] == 2


async def test_timesheet_admits_incomplete_data(
        portal: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Постраничность `task.elapseditem.getlist` на портале сломана.

    Когда объединение выборок с двух концов не сошлось с `total` из конверта,
    сумма — это минимум. Промолчать значило бы показать неверное число
    с уверенным видом (docs/00-portal-facts.md §5.2).
    """
    monkeypatch.setattr(views, "stages_of", _stages)

    async def partial(*_: object, **__: object) -> timesheet.Snapshot:
        return timesheet.Snapshot(tasks={}, entries=[], complete=False,
                                  seen=50, total_on_portal=79, at=_at(2026, 8, 20))

    monkeypatch.setattr(timesheet, "snapshot", partial)
    body = (await request("GET", f"{BASE}/timesheet?chat_ref={CHAT_REF}&month=2026-08",
                          auth=init_data())).json()
    assert body["complete"] is False
    assert (body["seen"], body["total_on_portal"]) == (50, 79)


def _at(year: int, month: int, day: int) -> Any:
    from datetime import UTC, datetime
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


# ------------------------------------------- списание: право и его отсутствие
async def test_timelog_allows_adding_when_the_portal_says_nothing(
        portal: FakeClient) -> None:  # noqa: F811
    """Право едет со списаниями, и отсутствие ключа — не запрет.

    Блок `action` живой задачи содержит `complete` и `edit`, а про списание
    молчит. Пока форма пряталась по наличию ключа, у человека не было ни формы,
    ни объяснения — «списать время» в приложении просто не существовало.
    """
    body = (await request("GET", f"{BASE}/tasks/100/timelog?chat_ref={CHAT_REF}",
                          auth=init_data())).json()
    assert body["can_add"] is True


async def test_timelog_form_is_hidden_only_on_an_explicit_refusal(
        portal: FakeClient) -> None:  # noqa: F811
    def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "tasks.task.get":
            return {"task": task_body(100, action={"edit": True,
                                                   "elapsedtime.add": False})}
        return [] if method == "task.elapseditem.getlist" else {"ok": True}

    portal.handler = handler
    body = (await request("GET", f"{BASE}/tasks/100/timelog?chat_ref={CHAT_REF}",
                          auth=init_data())).json()
    assert body["can_add"] is False


async def test_timelog_of_a_foreign_task_is_not_found(
        portal: FakeClient) -> None:  # noqa: F811
    """И-3: задача чужого проекта отвечает тем же 404, что и несуществующая."""
    def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "tasks.task.get":
            return {"task": task_body(100, groupId="999")}
        return [] if method == "task.elapseditem.getlist" else {"ok": True}

    portal.handler = handler
    res = await request("GET", f"{BASE}/tasks/100/timelog?chat_ref={CHAT_REF}",
                        auth=init_data())
    assert res.status_code == 404


# ---------------------------------------------------------------- опросник
def _questions(*_: object, **__: object) -> Any:
    async def call() -> list[survey.Question]:
        return [
            survey.Question("what", "Что случилось?", True),
            survey.Question("urgency", "Насколько срочно?", True, kind="choice",
                            options=[{"value": "2", "label": "Горит"},
                                     {"value": "0", "label": "Может подождать"}],
                            b24_field="PRIORITY", b24_field_type="integer"),
        ]

    return call()


async def test_survey_form_lists_questions(portal: FakeClient,  # noqa: F811
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "questions", _questions)
    body = (await request("GET", f"{BASE}/surveys/1?chat_ref={CHAT_REF}",
                          auth=init_data())).json()
    assert [q["code"] for q in body["items"]] == ["what", "urgency"]
    # Привязка к полю видна человеку: его ответ станет полем задачи, а не строкой
    # в описании, и знать об этом он вправе до того, как ответил.
    assert body["items"][1]["field"] == "PRIORITY"


async def test_survey_of_unknown_template_is_not_found(
        portal: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    async def none(*_: object, **__: object) -> list[survey.Question]:
        return []

    monkeypatch.setattr(survey, "questions", none)
    resp = await request("GET", f"{BASE}/surveys/777?chat_ref={CHAT_REF}",
                         auth=init_data())
    assert resp.status_code == 404


async def test_survey_answer_lands_in_the_task_field(
        portal: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Ответ на привязанный вопрос уходит в поле задачи, а не в описание.

    Разбирает это `survey.assemble` — тот же код, что и у бота. Иначе один и тот
    же набор вопросов давал бы в чате и в приложении две разные задачи.
    """
    monkeypatch.setattr(survey, "questions", _questions)
    created: dict[str, Any] = {}

    def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "tasks.task.add":
            created.update(params.get("fields") or {})
            return {"task": task_body(500)}
        if method == "tasks.task.list":
            return {"tasks": []}
        if method == "tasks.task.get":
            return {"task": task_body(500)}
        return {"ok": True}

    portal.handler = handler
    resp = await request(
        "POST", f"{BASE}/tasks?chat_ref={CHAT_REF}", auth=init_data(),
        json_body={"form_id": "surveyform01", "project_id": 5,
                   "survey_template_id": 1,
                   "answers": {"what": "Не печатает принтер", "urgency": "2"}})

    assert resp.status_code in (200, 201)
    assert created["PRIORITY"] == 2
    # Заголовок — ответ на первый непривязанный вопрос: он и есть суть обращения.
    assert created["TITLE"] == "Не печатает принтер"
    # Подпись варианта, а не служебное «2»: в описании человек должен узнать
    # то, что сам выбрал.
    assert "Горит" in created["DESCRIPTION"]


async def test_survey_without_required_answer_is_rejected(
        portal: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(survey, "questions", _questions)
    resp = await request(
        "POST", f"{BASE}/tasks?chat_ref={CHAT_REF}", auth=init_data(),
        json_body={"form_id": "surveyform02", "project_id": 5,
                   "survey_template_id": 1,
                   "answers": {"what": "Не печатает принтер"}})
    assert resp.status_code == 400
    assert "Насколько срочно" in resp.json()["error"]["message"]


# ---------------------------------------------------------------- вложения
async def test_upload_refuses_blocked_extension(portal: FakeClient) -> None:  # noqa: F811
    """Чёрный список расширений общий с ботом: «.exe» не уезжает ни оттуда, ни отсюда."""
    resp = await _upload(portal, [("files", ("вирус.exe", b"MZ", "application/exe"))])
    assert resp.status_code == 200
    assert resp.json()["attached"] == 0
    assert "не переносим" in resp.json()["rejected"][0]


async def test_upload_refuses_oversized_file(portal: FakeClient) -> None:  # noqa: F811
    big = b"x" * (20 * 1024 * 1024 + 1)
    resp = await _upload(portal, [("files", ("дамп.bin", big, "application/octet-stream"))])
    assert resp.json()["attached"] == 0
    assert "20 МБ" in resp.json()["rejected"][0]


async def test_upload_attaches_and_keeps_the_file(portal: FakeClient) -> None:  # noqa: F811
    uploaded: list[dict[str, Any]] = []

    def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "disk.storage.getlist":
            return [{"ID": "1", "ENTITY_TYPE": "group", "ENTITY_ID": str(GROUP_ID)}]
        if method == "disk.storage.getchildren":
            return [{"ID": "10", "TYPE": "folder"}]
        if method == "disk.folder.uploadfile":
            uploaded.append(params)
            return {"ID": "555", "NAME": params.get("data", {}).get("NAME", "")}
        if method == "tasks.task.get":
            return {"task": task_body(100)}
        return {"ok": True}

    portal.handler = handler
    resp = await _upload(portal, [("files", ("отчёт.pdf", b"%PDF-1.4", "application/pdf"))])
    assert resp.status_code == 200
    assert resp.json()["attached"] == 1
    assert len(uploaded) == 1
    # Привязка к задаче — обязательный второй шаг: загруженный на Диск файл
    # сам по себе к задаче не относится.
    assert any(m == "tasks.task.update" for m, _ in portal.calls)


async def test_upload_of_a_foreign_task_is_not_found(portal: FakeClient) -> None:  # noqa: F811
    """И-3 без исключений: файл нельзя приложить к задаче чужого проекта."""
    portal.handler = lambda method, params: (
        {"task": task_body(900, groupId="4242")} if method == "tasks.task.get"
        else {"ok": True})
    resp = await _upload(portal, [("files", ("а.txt", b"a", "text/plain"))], task_id=900)
    assert resp.status_code == 404


async def _upload(portal_client: FakeClient, files: Any, task_id: int = 100) -> Any:
    import httpx

    from b24bot.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.post(
            f"{BASE}/tasks/{task_id}/files?chat_ref={CHAT_REF}",
            headers={"Authorization": f"tma {init_data()}"},
            files=[(name, value) for name, value in files])


# ------------------------------------------------------------------ кто я
async def test_me_reports_portal_and_role(portal: FakeClient) -> None:  # noqa: F811
    body = (await request("GET", f"{BASE}/me", auth=init_data())).json()
    assert body["portal"] == "devondev.bitrix24.ru"
    assert body["b24_user_id"] > 0
    # Роль отвечается ВСЕГДА, пусть и умолчанием: экран «кто я» открывают ровно
    # тогда, когда что-то не работает, и пустое поле там бесполезно.
    assert body["role"]
    assert body["name"]
