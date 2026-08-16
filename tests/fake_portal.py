"""Контрактный дубль Битрикс24 на формах ответов, снятых с живого портала.

Все структуры здесь взяты из docs/00-portal-facts.md, а не придуманы: camelCase на
выходе, TAGS объектом, subStatus, блок time с operating, формат ответа batch.
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs

import httpx


def time_block(operating: float = 0.0, reset_in: float = 600.0) -> dict:
    now = time.time()
    return {
        "start": now, "finish": now + 0.12, "duration": 0.12, "processing": 0,
        "operating": operating, "operating_reset_at": now + reset_in,
    }


def task(task_id: int, *, status: int = 2, group_id: int = 33, stage_id: int = 335,
         title: str | None = None, deadline: str | None = None) -> dict:
    return {
        "id": str(task_id),
        "title": title or f"Задача {task_id}",
        "status": str(status),
        "subStatus": str(status),
        "stageId": str(stage_id),
        "groupId": str(group_id),
        "responsibleId": "1",
        "createdBy": "1",
        "priority": "1",
        "deadline": deadline,
        "createdDate": "2026-08-11T23:03:41+03:00",
        "changedDate": "2026-08-12T00:22:15+03:00",
        "closedDate": None,
        "tags": {"7": {"id": 7, "title": "devonbot"}},
        "action": {"complete": True, "start": True, "defer": False, "renew": False,
                   "delegate": True, "edit": True, "pause": False},
    }


class FakePortal:
    """Подставляется в B24Client как httpx-транспорт.

    Умеет: обычные вызовы, batch, keyset-пагинацию, и главное — воспроизводить
    отказы портала (лимиты, мёртвый токен, нет прав), потому что именно на них
    ломается реальный код.
    """

    def __init__(self, *, tasks: int = 0, fail_times: int = 0,
                 fail_code: str = "QUERY_LIMIT_EXCEEDED", fail_status: int = 503,
                 operating: float = 0.0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._tasks = [task(100 + i * 2) for i in range(tasks)]
        self._fail_left = fail_times
        self._fail_code = fail_code
        self._fail_status = fail_status
        self._operating = operating

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def method_calls(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)

    # ------------------------------------------------------------------ router
    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        params = {k: v[0] if len(v) == 1 else v
                  for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((method, params))

        if self._fail_left > 0:
            self._fail_left -= 1
            return httpx.Response(self._fail_status, json={
                "error": self._fail_code,
                "error_description": "воспроизведение отказа портала",
                "time": time_block(self._operating),
            })

        if method == "batch":
            return self._batch(params)
        if method == "tasks.task.list":
            return self._task_list(params)
        if method == "tasks.task.get":
            tid = int(params.get("taskId", 0))
            return self._ok({"task": task(tid)})
        if method == "user.current":
            return self._ok({"ID": "1", "NAME": "Сергей", "LAST_NAME": "Крищунс"})
        return self._ok({"ok": True, "method": method})

    # ----------------------------------------------------------------- handlers
    def _ok(self, result: Any, total: int | None = None) -> httpx.Response:
        body: dict[str, Any] = {"result": result, "time": time_block(self._operating)}
        if total is not None:
            body["total"] = total
        return httpx.Response(200, json=body)

    def _task_list(self, params: dict) -> httpx.Response:
        after = None
        for key, value in params.items():
            if key.startswith("filter[>ID]"):
                after = int(value)
        items = [t for t in self._tasks if after is None or int(t["id"]) > after]
        page = items[:50]
        return self._ok({"tasks": page}, total=len(self._tasks))

    def _batch(self, params: dict) -> httpx.Response:
        results: dict[str, Any] = {}
        errors: dict[str, Any] = {}
        totals: dict[str, Any] = {}
        for key, raw in params.items():
            if not key.startswith("cmd["):
                continue
            name = key[4:-1]
            method = raw.split("?", 1)[0]
            if method == "tasks.task.get":
                tid = int(parse_qs(raw.split("?", 1)[1]).get("taskId", ["0"])[0])
                results[name] = {"task": task(tid)}
            elif method == "unknown.method":
                errors[name] = {"error": "ERROR_METHOD_NOT_FOUND",
                                "error_description": "метода нет"}
            else:
                results[name] = {"ok": True}
                totals[name] = 0
        return self._ok({"result": results, "result_error": errors,
                         "result_total": totals, "result_next": {}})


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
