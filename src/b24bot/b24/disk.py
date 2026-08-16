"""Загрузка файлов на Диск Битрикса и привязка к задаче.

Путь проверен на живом портале (docs/00-portal-facts.md §9.4):

    disk.storage.getlist(ENTITY_TYPE=group, ENTITY_ID=<группа>) -> хранилище
    disk.storage.getchildren(<хранилище>)                       -> папка
    disk.folder.uploadfile(id=<папка>, fileContent=<base64>)     -> файл
    tasks.task.update(UF_TASK_WEBDAV_FILES=["n<ID файла>"])      -> привязка

Две ловушки. При записи в поле идёт `n<ID файла>`, а читается оттуда **ID
присоединённого объекта** — это разные числа, и `disk.file.get` по прочитанному
значению вернёт `ERROR_NOT_FOUND`. И хранилище группы появляется не сразу: у пустой
группы его может не быть, тогда падаем на личный диск пользователя.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from b24bot.b24 import errors
from b24bot.b24.client import B24Client
from b24bot.core.text import safe_filename

log = logging.getLogger(__name__)


async def group_folder(client: B24Client, group_id: int) -> int | None:
    """Папка для загрузок в хранилище проекта."""
    storages = await client.call("disk.storage.getlist", {
        "filter": {"ENTITY_TYPE": "group", "ENTITY_ID": group_id}})
    mine = [s for s in (storages or []) if str(s.get("ENTITY_ID")) == str(group_id)]
    if not mine:
        return None

    storage = mine[0]
    children = await client.call("disk.storage.getchildren", {"id": storage["ID"]})
    for child in children or []:
        if child.get("TYPE") == "folder":
            return int(child["ID"])
    root = storage.get("ROOT_OBJECT_ID")
    return int(root) if root else None


async def user_folder(client: B24Client, b24_user_id: int) -> int | None:
    """Запасной путь: личный диск. Хранилища группы может не быть у пустой группы."""
    storages = await client.call("disk.storage.getlist", {
        "filter": {"ENTITY_TYPE": "user", "ENTITY_ID": b24_user_id}})
    mine = [s for s in (storages or []) if str(s.get("ENTITY_ID")) == str(b24_user_id)]
    if not mine:
        return None
    root = mine[0].get("ROOT_OBJECT_ID")
    return int(root) if root else None


async def upload(client: B24Client, folder_id: int, name: str,
                 content: bytes) -> dict[str, Any]:
    payload = base64.b64encode(content).decode()
    res = await client.call("disk.folder.uploadfile", {
        "id": folder_id,
        "data": {"NAME": safe_filename(name)},
        "fileContent": payload,
    })
    return res if isinstance(res, dict) else {}


async def attach_to_task(client: B24Client, task_id: int,
                         file_ids: list[int]) -> None:
    """Привязать загруженные файлы к задаче, не потеряв уже прикреплённые."""
    if not file_ids:
        return

    current: list[str] = []
    try:
        res = await client.call("tasks.task.get", {
            "taskId": task_id, "select": ["ID", "UF_TASK_WEBDAV_FILES"]})
        task = res.get("task", res) if isinstance(res, dict) else {}
        # В ответе лежат ID присоединённых объектов, а на запись нужны ID файлов.
        # Смешивать нельзя, поэтому существующие значения переносим как есть.
        current = [str(x) for x in (task.get("ufTaskWebdavFiles") or [])]
    except errors.B24Error as exc:
        log.warning("не удалось прочитать вложения задачи %s: %s", task_id, exc)

    values = current + [f"n{fid}" for fid in file_ids]
    await client.call("tasks.task.update", {
        "taskId": task_id, "fields": {"UF_TASK_WEBDAV_FILES": values}})
