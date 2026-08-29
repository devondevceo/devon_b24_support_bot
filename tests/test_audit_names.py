"""Имена в журнале действий: одно действие — одно имя во всех точках входа.

Журнал нужен для разбора «кто закрыл задачу», а разбор идёт по имени действия.
Если бот пишет `task.complete`, а мини-апп за ту же кнопку — `task.finish`, то
выборка по имени врёт, ничем себя не выдавая: записи есть, они просто разные.
Поэтому наборы действий обеих точек входа сверяются здесь, а не на глаз.

Второй страж — поведенческий: мало договориться об именах, надо ещё писать.
Действия бота меняют состояние задачи на портале и до 18.08.2026 не писались вовсе.
"""
from __future__ import annotations

from typing import Any

import pytest

from b24bot.api import miniapp as api_miniapp
from b24bot.b24 import errors
from b24bot.bot import handlers, texts
from b24bot.domain import access, audit
from b24bot.domain import events as b24_events
from b24bot.domain.context import ChatContext, ProjectRef

TENANT = 1
TG_USER = 42
B24_USER = 7
CHAT_REF = 11
TASK_ID = 100

PROJECT = ProjectRef(id=5, b24_group_id=33, name="Поддержка сайта",
                     client_name="Линия Жизни")


# ----------------------------------------------------------------- сверка имён
def test_every_bot_action_has_an_audit_name() -> None:
    """Действие без имени в журнале — действие, которого в журнале нет."""
    assert set(handlers.ACTION_METHODS) == set(handlers.ACTION_AUDIT)
    assert set(handlers.ACTION_METHODS) == set(handlers.ACTION_STATUS)


def test_every_miniapp_action_has_an_audit_name() -> None:
    assert set(api_miniapp.ACTIONS) == set(api_miniapp.AUDIT_ACTIONS)
    assert set(api_miniapp.ACTIONS) == set(api_miniapp.ACTION_STATUS)


def test_shared_actions_mean_the_same_thing_everywhere() -> None:
    """Главный страж: общее действие — один код аудита, один метод, один статус."""
    shared = set(handlers.ACTION_METHODS) & set(api_miniapp.ACTIONS)
    assert shared, "точки входа разошлись до неузнаваемости"
    for act in sorted(shared):
        assert handlers.ACTION_AUDIT[act] == api_miniapp.AUDIT_ACTIONS[act], act
        assert handlers.ACTION_METHODS[act] == api_miniapp.ACTIONS[act], act
        assert handlers.ACTION_STATUS[act] == api_miniapp.ACTION_STATUS[act], act


def test_shared_edit_fields_mean_the_same_thing_everywhere() -> None:
    """То же самое для правки полей: бот правит их кнопками, мини-апп — формой."""
    for field, action in handlers.EDIT_AUDIT.items():
        assert api_miniapp.AUDIT_FIELDS[field] == action, field


def test_high_risk_actions_are_declared_high_risk() -> None:
    """`task.complete` и `task.defer` числятся в списке высокого риска
    (docs/40-security.md §3). Имя, которого нет в `HIGH_RISK`, в будущем режиме
    `minimal` пишется без `detail` — то есть теряется ровно там, где нужно."""
    used = set(handlers.ACTION_AUDIT.values()) | set(api_miniapp.AUDIT_ACTIONS.values())
    assert {"task.complete", "task.defer"} <= used
    assert {"task.complete", "task.defer"} <= audit.HIGH_RISK
    assert "task.responsible.change" in audit.HIGH_RISK


# ------------------------------------------------------------- поведение бота
class FakeClient:
    """Портал: отвечает задачей, при `fail` роняет мутацию, но не чтение."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params or {}))
        if method == "tasks.task.get":
            return {"task": {"id": str(TASK_ID), "groupId": str(PROJECT.b24_group_id)}}
        if self.fail is not None:
            raise self.fail
        return {"task": {"id": str(TASK_ID)}}


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Окружение действия подменено целиком: интерес здесь к записи в журнал."""
    records: list[dict[str, Any]] = []

    async def record(tenant_id: int, action: str, **kw: Any) -> None:
        records.append({"tenant_id": tenant_id, "action": action, **kw})

    async def linked(*_: object) -> int:
        return B24_USER

    async def authorize(*_: object, **__: object) -> ProjectRef:
        return PROJECT

    async def noop(*_: object, **__: object) -> None:
        return None

    async def card(*_: object, **__: object) -> handlers.Reply:
        return handlers.Reply("карточка")

    monkeypatch.setattr(audit, "record", record)
    monkeypatch.setattr(access, "linked_b24_user", linked)
    monkeypatch.setattr(handlers, "authorize_task_for_chat", authorize)
    monkeypatch.setattr(b24_events, "suppress_echo", noop)
    monkeypatch.setattr(handlers, "_open_card", card)
    return records


def _portal(monkeypatch: pytest.MonkeyPatch,
            fail: Exception | None = None) -> FakeClient:
    client = FakeClient(fail)

    async def for_user(*_: object, **__: object) -> FakeClient:
        return client

    monkeypatch.setattr(access, "client_for_user", for_user)
    return client


def _ctx() -> ChatContext:
    return ChatContext(chat_ref=CHAT_REF, chat_id=-1001, title="Поддержка",
                       status="active", tenant_id=TENANT, is_forum=False,
                       projects=[PROJECT])


@pytest.mark.parametrize("act", sorted(api_miniapp.ACTIONS))
async def test_bot_writes_the_same_audit_name_as_miniapp(
        act: str, written: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Каждое действие, которое бот умеет, пишется именем мини-аппа.

    Параметризация идёт по действиям мини-аппа, а не бота: так новая кнопка в боте
    сразу попадает под проверку, а не ждёт, пока кто-то вспомнит про этот файл.
    """
    client = _portal(monkeypatch)
    reply = await handlers._task_action(_ctx(), TG_USER, TASK_ID, act)

    if reply.text == texts.MSG_DIALOG_EXPIRED:
        pytest.skip(f"бот пока не предлагает действие {act}")

    assert (api_miniapp.ACTIONS[act], {"taskId": TASK_ID}) in client.calls
    assert len(written) == 1
    entry = written[0]
    assert entry["action"] == api_miniapp.AUDIT_ACTIONS[act]
    assert entry["tenant_id"] == TENANT
    assert entry["actor_id"] == B24_USER
    assert entry["actor_tg_id"] == TG_USER
    assert entry["target"] == f"task:{TASK_ID}"
    assert entry["project_id"] == PROJECT.id
    assert entry["detail"] == {"act": act, "source": "bot"}


async def test_refresh_is_not_a_mutation(written: list[dict[str, Any]],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Обновление карточки ничего не меняет — и в журнале ему делать нечего."""
    _portal(monkeypatch)
    await handlers._task_action(_ctx(), TG_USER, TASK_ID, "refresh")
    assert written == []


async def test_failed_mutation_is_not_written(written: list[dict[str, Any]],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Запись о том, чего не произошло, хуже её отсутствия: по ней ищут виноватого."""
    _portal(monkeypatch, errors.B24AccessDenied("ACCESS_DENIED", "нет прав"))
    reply = await handlers._task_action(_ctx(), TG_USER, TASK_ID, "complete")
    assert "нет прав" in reply.text
    assert written == []


async def test_foreign_task_is_not_written(written: list[dict[str, Any]],
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """И-3: задача чужого чата не мутируется — значит, и в журнал не попадает."""
    _portal(monkeypatch)

    async def denied(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(handlers, "authorize_task_for_chat", denied)
    reply = await handlers._task_action(_ctx(), TG_USER, TASK_ID, "complete")
    assert reply.text == texts.MSG_TASK_NOT_FOUND
    assert written == []


# --------------------------------------------------------------- трудозатраты
def test_time_logging_has_one_name_for_both_doors() -> None:
    """Списание времени называется одинаково из бота и из мини-аппа.

    У остальных действий имена сверяются списками: наборы там разные. Здесь
    действие одно, и общая константа `timelog.AUDIT_ACTION` делает расхождение
    невозможным по построению — тест сторожит, что литерал не вернулся обратно
    в код по частям.
    """
    import inspect

    from b24bot.domain import timelog

    assert timelog.AUDIT_ACTION == "task.time.log"
    for module in (handlers, api_miniapp):
        source = inspect.getsource(module)
        assert '"task.time.log"' not in source, module.__name__
        assert "AUDIT_ACTION" in source, module.__name__
