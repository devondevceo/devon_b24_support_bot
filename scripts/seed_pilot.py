"""Заведение пилотного клиента и импорт его проектов из Битрикса.

Позже это делается через интерфейс приложения; сейчас — чтобы сквозной сценарий
можно было проверить целиком.
"""
from __future__ import annotations

import asyncio
import sys

from b24bot.db.pool import close_pool, init_pool, pool
from b24bot.domain import access

TENANT_ID = 1
CLIENT_NAME = "Линия Жизни"
GROUP_IDS = [33]


async def main() -> int:
    await init_pool()
    client = await access.client_for_service(TENANT_ID)

    async with client:
        groups = await client.call("sonet_group.get", {"ORDER": {"ID": "ASC"}})
        by_id = {int(g["ID"]): g for g in groups}

        async with pool().acquire() as conn:
            cid = await conn.fetchval(
                "INSERT INTO clients (tenant_id, name) VALUES ($1,$2) "
                "ON CONFLICT (tenant_id, name) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id", TENANT_ID, CLIENT_NAME)
            print(f"клиент «{CLIENT_NAME}» -> id={cid}")

            for gid in GROUP_IDS:
                g = by_id.get(gid)
                if g is None:
                    print(f"  группа {gid} на портале не найдена")
                    continue
                pid = await conn.fetchval(
                    """
                    INSERT INTO projects (tenant_id, client_id, b24_group_id, name,
                                          is_extranet, owner_b24_user_id, name_synced_at)
                    VALUES ($1,$2,$3,$4,$5,$6,now())
                    ON CONFLICT (tenant_id, b24_group_id) DO UPDATE
                      SET name = EXCLUDED.name, client_id = EXCLUDED.client_id,
                          status = 'active', name_synced_at = now()
                    RETURNING id
                    """,
                    TENANT_ID, cid, gid, g["NAME"], g.get("IS_EXTRANET") == "Y",
                    int(g.get("OWNER_ID") or 0) or None)
                print(f"  проект {gid} «{g['NAME']}» -> id={pid}")

                stages = await client.call("task.stages.get", {"entityId": gid})
                for st in (stages or {}).values():
                    await conn.execute(
                        """
                        INSERT INTO project_stages (tenant_id, project_id, b24_stage_id,
                                                    title, sort, system_type, color)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (tenant_id, project_id, b24_stage_id) DO UPDATE
                          SET title = EXCLUDED.title, sort = EXCLUDED.sort,
                              synced_at = now()
                        """,
                        TENANT_ID, pid, int(st["ID"]), st["TITLE"], int(st.get("SORT") or 0),
                        st.get("SYSTEM_TYPE"), st.get("COLOR"))
                print(f"     стадий: {len(stages or {})}")

    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
