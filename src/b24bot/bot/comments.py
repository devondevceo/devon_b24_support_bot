"""Комментарии к задаче и перенос вложений.

Комментарии на этом портале живут в ЧАТЕ задачи, а не в форуме
(docs/00-portal-facts.md §5.1). Поэтому:

* писать — `task.commentitem.add`, он работает и возвращает ID сообщения чата;
* читать — только `im.dialog.messages.get` по `chat<CHAT_ID>`;
* `task.commentitem.getlist` всегда пуст, а `task.commentitem.get` отдаёт
  `ITEM_NOT_FOUND_OR_NOT_ACCESSIBLE` — пользоваться ими нельзя;
* системные записи лежат там же, у них `author_id = 0`, и их надо отфильтровывать.
"""
from __future__ import annotations

import logging
from typing import Any

from b24bot.b24 import disk, errors
from b24bot.b24.client import B24Client
from b24bot.core.text import esc_bbcode, esc_html, safe_filename
from b24bot.db.pool import pool
from b24bot.tg import files as tg_files

log = logging.getLogger(__name__)

DISCUSSION_LIMIT = 10


async def add(client: B24Client, task_id: int, text: str, *, author: str,
              chat_title: str) -> int | None:
    """Комментарий от имени действующего пользователя (ходим его токеном)."""
    body = (f"{esc_bbcode(text)}\n\n"
            f"[i]— из Telegram, чат «{esc_bbcode(chat_title)}», {esc_bbcode(author)}[/i]")
    result = await client.call("task.commentitem.add", {
        "TASKID": task_id, "FIELDS": {"POST_MESSAGE": body}})
    return int(result) if isinstance(result, int | str) and str(result).isdigit() else None


async def read_discussion(client: B24Client, task_id: int) -> list[dict[str, Any]]:
    """Обсуждение задачи. Системные сообщения отбрасываются."""
    res = await client.call("tasks.task.get", {
        "taskId": task_id, "select": ["ID", "CHAT_ID"]})
    task = res.get("task", res) if isinstance(res, dict) else {}
    chat_id = task.get("chatId")
    if not chat_id:
        return []

    try:
        dialog = await client.call("im.dialog.messages.get",
                                   {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50})
    except errors.B24Error as exc:
        log.info("не удалось прочитать чат задачи %s: %s", task_id, exc)
        return []

    messages = (dialog or {}).get("messages", []) if isinstance(dialog, dict) else []
    users = {str(u.get("id")): u for u in (dialog or {}).get("users", [])}

    out = []
    for m in messages:
        # author_id = 0 — системная запись «создал задачу», «изменил статус».
        author_id = str(m.get("author_id") or "0")
        if author_id == "0":
            continue
        user = users.get(author_id) or {}
        out.append({
            "id": m.get("id"),
            "author": user.get("name") or f"пользователь {author_id}",
            "text": str(m.get("text") or ""),
            "date": m.get("date"),
        })
    return out[-DISCUSSION_LIMIT:]


def render_discussion(task_id: int, items: list[dict[str, Any]]) -> str:
    if not items:
        return (f"<b>#{task_id}</b> · обсуждение\n\n"
                "Пока никто ничего не написал.")
    rows = [f"<b>#{task_id}</b> · обсуждение, последние {len(items)}", ""]
    for m in items:
        rows.append(f"<b>{esc_html(m['author'])}</b>")
        rows.append(esc_html(m["text"][:500]))
        rows.append("")
    return "\n".join(rows).strip()


# ------------------------------------------------------------------ вложения
async def transfer_files(client: B24Client, bot_token: str, tenant_id: int,
                         task_id: int, group_id: int, b24_user_id: int,
                         attachments: list[tg_files.Attachment],
                         idem_prefix: str) -> tuple[int, list[str]]:
    """Перенести файлы из Telegram в задачу. Возвращает (сколько, список отказов)."""
    if not attachments:
        return 0, []

    folder = await disk.group_folder(client, group_id)
    if folder is None:
        folder = await disk.user_folder(client, b24_user_id)
    if folder is None:
        return 0, ["не нашлось хранилища для файлов"]

    uploaded: list[int] = []
    rejected: list[str] = []
    total = 0

    for att in attachments[:tg_files.MAX_PER_TASK]:
        name = safe_filename(att.name)
        # Файл со статусом uploaded не перезаливается никогда (инвариант И-10).
        async with pool().acquire() as conn:
            done = await conn.fetchval(
                "SELECT b24_file_id FROM tg_attachments WHERE tenant_id = $1 "
                "AND idem_key = $2 AND tg_file_id = $3 AND state = 'uploaded'",
                tenant_id, idem_prefix, att.file_id)
        if done:
            uploaded.append(int(done))
            continue

        try:
            content = await tg_files.download(bot_token, att)
        except tg_files.FileTooBig:
            rejected.append(f"«{name}» больше 20 МБ")
            continue
        except tg_files.FileBlocked:
            rejected.append(f"«{name}» — этот тип файла не переносим")
            continue
        except Exception as exc:
            log.warning("не удалось скачать %s: %s", name, str(exc)[:120])
            rejected.append(f"«{name}» не скачался")
            continue

        total += len(content)
        if total > tg_files.TOTAL_PER_TASK:
            rejected.append(f"«{name}» — превышен общий объём на задачу")
            break

        try:
            result = await disk.upload(client, folder, name, content)
        except errors.B24Error as exc:
            log.warning("не удалось загрузить %s: %s", name, exc)
            rejected.append(f"«{name}» не загрузился в Битрикс24")
            continue

        file_id = int(result.get("ID") or 0)
        if not file_id:
            rejected.append(f"«{name}» не загрузился в Битрикс24")
            continue

        uploaded.append(file_id)
        async with pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO tg_attachments (tenant_id, idem_key, tg_file_id, file_name, "
                "size_bytes, state, b24_file_id) VALUES ($1,$2,$3,$4,$5,'uploaded',$6) "
                "ON CONFLICT (tenant_id, idem_key, tg_file_id) DO UPDATE "
                "SET state = 'uploaded', b24_file_id = EXCLUDED.b24_file_id",
                tenant_id, idem_prefix, att.file_id, name, len(content), file_id)

    if uploaded:
        await disk.attach_to_task(client, task_id, uploaded)
    return len(uploaded), rejected
