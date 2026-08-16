"""HTTP-контракт мини-аппа: доступ, изоляция, отказы.

Проверяется то, что ломается тихо: подпись без чата, чат чужого теннанта, задача
чужого проекта, выборка без границы по группам, дата без часового пояса. Портал и
база подменены — интерес здесь к правилам доступа, а не к их окружению.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from b24bot.api import miniapp as api_miniapp
from b24bot.api.main import app
from b24bot.b24 import errors
from b24bot.bot import task_create, views
from b24bot.domain import access
from b24bot.domain import events as b24_events
from b24bot.domain import miniapp as domain_miniapp
from b24bot.domain.context import TASK_NOT_FOUND, ChatContext, ProjectRef
from b24bot.tg import initdata

TOKEN = "123456789:AAHfake-token-for-tests-only-0000000"
TENANT = 1
B24_USER = 7
CHAT_REF = 11
GROUP_ID = 33

PROJECT = ProjectRef(id=5, b24_group_id=GROUP_ID, name="Поддержка сайта",
                     client_name="Линия Жизни")


def init_data(**overrides: object) -> str:
    fields: dict[str, str] = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": 42, "first_name": "Иван", "username": "ivanov"},
                           ensure_ascii=False),
    }
    fields.update({k: str(v) for k, v in overrides.items()})
    return initdata.sign(fields, TOKEN)


def task_body(task_id: int = 100, *, group_id: int = GROUP_ID, **extra: Any) -> dict[str, Any]:
    body = {
        "id": str(task_id), "title": "Тестовая задача", "status": "2",
        "stageId": "335", "groupId": str(group_id), "responsibleId": "7",
        "createdBy": "7", "priority": "1", "deadline": None,
        "createdDate": "2026-08-11T23:03:41+03:00",
        "action": {"complete": True, "edit": True},
    }
    body.update(extra)
    return body


class FakeClient:
    """Портал: помнит вызовы и отвечает заранее заданным."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params or {}))
        return self.handler(method, params or {})

    async def call_many(self, calls: Any) -> dict[str, Any]:
        return {key: [{"ID": str(params["ID"]), "NAME": "Пётр", "LAST_NAME": "Петров"}]
                for key, _method, params in calls}

    def params_of(self, method: str) -> dict[str, Any]:
        return next(p for m, p in self.calls if m == method)


@pytest.fixture
def portal(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Подменяет портал, базу и опознание пользователя разом."""
    client = FakeClient(lambda method, params: default_portal(method, params))

    async def candidates(*_: object) -> list[domain_miniapp.Bot]:
        return [domain_miniapp.Bot(TENANT, 123456789, "devon_sd_bot", TOKEN)]

    async def linked(tenant_id: int, tg_user_id: int) -> int | None:
        return B24_USER if tenant_id == TENANT else None

    async def chat(chat_ref: int, thread_id: int | None = None) -> ChatContext | None:
        # Чат 99 принадлежит другому теннанту — на нём проверяется изоляция.
        return ChatContext(chat_ref=chat_ref, chat_id=-100123, title="ЛЖ · поддержка",
                           status="active", tenant_id=TENANT if chat_ref == CHAT_REF else 2,
                           is_forum=False, projects=[PROJECT])

    async def authorize(tenant_id: int, chat_ref: int, task_id: int, *,
                        group_id_hint: int | None = None) -> ProjectRef | None:
        return PROJECT if group_id_hint in (None, GROUP_ID) else None

    async def noop(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(domain_miniapp, "_candidates", candidates)
    monkeypatch.setattr(access, "linked_b24_user", linked)
    monkeypatch.setattr(domain_miniapp, "load_chat_context_by_ref", chat)
    monkeypatch.setattr(api_miniapp, "authorize_task_for_chat", authorize)
    monkeypatch.setattr(api_miniapp, "remember_task", noop)
    monkeypatch.setattr(b24_events, "suppress_echo", noop)
    monkeypatch.setattr(api_miniapp, "_client", lambda actor: _ready(client))
    for module in (api_miniapp, views, task_create):
        monkeypatch.setattr(module, "pool", lambda: FakePool())
    return client


def _ready(client: FakeClient) -> Any:
    async def wrapper() -> FakeClient:
        return client

    return wrapper()


class FakePool:
    """База: отвечает пусто на всё, кроме домена портала."""

    def acquire(self) -> FakePool:
        return self

    def transaction(self) -> FakePool:
        return self

    async def __aenter__(self) -> FakePool:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetchval(self, *_: object) -> str:
        return "devondev.bitrix24.ru"

    async def fetch(self, *_: object) -> list[Any]:
        return []

    async def fetchrow(self, *_: object) -> None:
        return None

    async def execute(self, *_: object) -> None:
        return None


def default_portal(method: str, params: dict[str, Any]) -> Any:
    if method == "tasks.task.list":
        return {"tasks": [task_body(100), task_body(101)]}
    if method == "tasks.task.get":
        return {"task": task_body(int(params.get("taskId", 100)))}
    return {"ok": True}


async def request(method: str, url: str, *, auth: str | None = None,
                  json_body: Any = None) -> httpx.Response:
    headers = {"Authorization": f"tma {auth}"} if auth is not None else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.request(method, url, headers=headers, json=json_body)


# ------------------------------------------------------------------ доступ
async def test_without_header_unauthenticated(portal: FakeClient) -> None:
    resp = await request("GET", f"/api/miniapp/tasks?chat_ref={CHAT_REF}")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


async def test_foreign_signature_rejected(portal: FakeClient) -> None:
    """Подпись чужим токеном не должна опознаваться как наш пользователь."""
    foreign = initdata.sign({"auth_date": str(int(time.time())),
                             "user": json.dumps({"id": 42})}, "999:otherbottoken000")
    resp = await request("GET", f"/api/miniapp/tasks?chat_ref={CHAT_REF}", auth=foreign)
    assert resp.status_code == 401


async def test_chat_of_another_tenant_forbidden(portal: FakeClient) -> None:
    """Инвариант И-2: чужой чат недоступен, даже если знать его номер."""
    resp = await request("GET", "/api/miniapp/tasks?chat_ref=99", auth=init_data())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_forged_start_param_rejected(portal: FakeClient) -> None:
    packed = domain_miniapp.pack_context(CHAT_REF)
    forged = packed.replace(f"{CHAT_REF}-", "99-", 1)
    resp = await request("GET", "/api/miniapp/tasks", auth=init_data(start_param=forged))
    assert resp.status_code == 403


async def test_signed_start_param_pins_chat(portal: FakeClient) -> None:
    packed = domain_miniapp.pack_context(CHAT_REF)
    resp = await request("GET", "/api/miniapp/tasks", auth=init_data(start_param=packed))
    assert resp.status_code == 200
    body = resp.json()
    assert body["context"]["chat_ref"] == CHAT_REF
    assert body["context"]["pinned"] is True


# -------------------------------------------------------------------- список
async def test_list_is_bounded_by_group_ids(portal: FakeClient) -> None:
    """Область выборки задаёт ТОЛЬКО фильтр по группам — сервисной учётки нет."""
    resp = await request("GET", f"/api/miniapp/tasks?chat_ref={CHAT_REF}",
                         auth=init_data())
    assert resp.status_code == 200
    assert portal.params_of("tasks.task.list")["filter"]["GROUP_ID"] == [GROUP_ID]


async def test_closed_filter_asks_for_closed(portal: FakeClient) -> None:
    await request("GET", f"/api/miniapp/tasks?chat_ref={CHAT_REF}&filter=closed",
                  auth=init_data())
    flt = portal.params_of("tasks.task.list")["filter"]
    assert flt["REAL_STATUS"] == 5
    assert "!=REAL_STATUS" not in flt


async def test_unknown_filter_rejected(portal: FakeClient) -> None:
    resp = await request("GET", f"/api/miniapp/tasks?chat_ref={CHAT_REF}&filter=прочее",
                         auth=init_data())
    assert resp.status_code == 400


# ------------------------------------------------------------------ карточка
async def test_task_of_another_project_not_found(portal: FakeClient) -> None:
    """Инвариант И-3: задача чужого проекта отвечает ровно тем же отказом."""
    portal.handler = lambda method, params: (
        {"task": task_body(500, group_id=77)} if method == "tasks.task.get"
        else default_portal(method, params))
    resp = await request("GET", f"/api/miniapp/tasks/500?chat_ref={CHAT_REF}",
                         auth=init_data())
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == TASK_NOT_FOUND


async def test_task_denied_by_portal_looks_the_same(portal: FakeClient) -> None:
    """Нет прав и нет задачи обязаны отвечать одинаково, иначе это оракул."""
    def deny(method: str, params: dict[str, Any]) -> Any:
        if method == "tasks.task.get":
            raise errors.B24AccessDenied("ACCESS_DENIED", "нет прав", method)
        return default_portal(method, params)

    portal.handler = deny
    resp = await request("GET", f"/api/miniapp/tasks/500?chat_ref={CHAT_REF}",
                         auth=init_data())
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == TASK_NOT_FOUND


# --------------------------------------------------------------- изменение
async def test_deadline_without_timezone_rejected(portal: FakeClient) -> None:
    """Дата без offset у каждого портала своя — угадывать её мы не имеем права."""
    resp = await request("PATCH", f"/api/miniapp/tasks/100?chat_ref={CHAT_REF}",
                         auth=init_data(), json_body={"deadline": "2026-08-20T18:00:00"})
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["field"] == "deadline"


async def test_unknown_field_rejected(portal: FakeClient) -> None:
    resp = await request("PATCH", f"/api/miniapp/tasks/100?chat_ref={CHAT_REF}",
                         auth=init_data(), json_body={"status": 5})
    assert resp.status_code == 400


async def test_responsible_change_uses_delegate(portal: FakeClient) -> None:
    """Смена ответственного — спец-методом: он проводит права и уведомления."""
    portal.handler = lambda method, params: (
        {"task": task_body(100, responsibleId="9")} if method == "tasks.task.get"
        else default_portal(method, params))
    resp = await request("PATCH", f"/api/miniapp/tasks/100?chat_ref={CHAT_REF}",
                         auth=init_data(), json_body={"responsible_id": 9})
    assert resp.status_code == 200
    assert portal.params_of("tasks.task.delegate") == {"taskId": 100, "userId": 9}
    assert resp.json()["not_applied"] == []


async def test_silently_ignored_field_is_reported(portal: FakeClient) -> None:
    """Битрикс молча игнорирует непринятое. Молчать вслед за ним нельзя."""
    resp = await request("PATCH", f"/api/miniapp/tasks/100?chat_ref={CHAT_REF}",
                         auth=init_data(), json_body={"priority": 2})
    assert resp.status_code == 200
    # Портал вернул priority=1, хотя просили 2.
    assert resp.json()["not_applied"] == ["приоритет"]


async def test_portal_denial_becomes_403(portal: FakeClient) -> None:
    def deny(method: str, params: dict[str, Any]) -> Any:
        if method == "tasks.task.update":
            raise errors.B24AccessDenied("ACCESS_DENIED", "нельзя менять срок", method)
        return default_portal(method, params)

    portal.handler = deny
    resp = await request("PATCH", f"/api/miniapp/tasks/100?chat_ref={CHAT_REF}",
                         auth=init_data(),
                         json_body={"deadline": "2026-08-20T18:00:00+03:00"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "b24_forbidden"


# --------------------------------------------------------------- создание
async def test_create_requires_form_id(portal: FakeClient) -> None:
    """Без ключа идемпотентности ретрай создаст дубль (И-10)."""
    resp = await request("POST", f"/api/miniapp/tasks?chat_ref={CHAT_REF}",
                         auth=init_data(), json_body={"title": "Не работает почта"})
    assert resp.status_code == 400


async def test_create_passes_full_form(portal: FakeClient) -> None:
    calls: list[dict[str, Any]] = []

    def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "tasks.task.add":
            calls.append(params["fields"])
            return {"task": task_body(200)}
        if method == "tasks.task.list":
            return {"tasks": []}  # поиск по ключу идемпотентности: ничего нет
        return default_portal(method, params)

    portal.handler = handler
    resp = await request(
        "POST", f"/api/miniapp/tasks?chat_ref={CHAT_REF}", auth=init_data(),
        json_body={"form_id": "abcdef0123456789", "project_id": PROJECT.id,
                   "title": "Не работает почта", "description": "Подробности [тут]",
                   "deadline": "2026-08-20T18:00:00+03:00", "priority": 2,
                   "responsible_id": 9, "stage_id": 337})
    assert resp.status_code == 201
    fields = calls[0]
    assert fields["RESPONSIBLE_ID"] == 9
    assert fields["DEADLINE"] == "2026-08-20T18:00:00+03:00"
    assert fields["PRIORITY"] == 2
    assert fields["STAGE_ID"] == 337
    assert fields["GROUP_ID"] == GROUP_ID
    # И-6: квадратные скобки в тексте человека Битрикс съедает как BBCode.
    assert "[тут]" not in fields["DESCRIPTION"]
    assert "［тут］" in fields["DESCRIPTION"]


# --------------------------------------------------------------- комментарии
async def test_comment_requires_text(portal: FakeClient) -> None:
    resp = await request("POST", f"/api/miniapp/tasks/100/comments?chat_ref={CHAT_REF}",
                         auth=init_data(), json_body={"text": "   "})
    assert resp.status_code == 400
