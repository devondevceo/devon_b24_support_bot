"""Триггер «один чат — один клиент» проверяет только активные привязки

Дефект нашёлся при попытке ОТКЛЮЧИТЬ ошибочную привязку: триггер срабатывал на любом
UPDATE, включая перевод строки в 'disabled', и отказывал — то есть неверную привязку
нельзя было снять штатным способом. Проверять имеет смысл только то, что становится
или остаётся активным.

Revision ID: 0005_fix_binding_trigger
Revises: 0004_bot_runtime
"""
from alembic import op

revision = "0005_fix_binding_trigger"
down_revision = "0004_bot_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION chat_bindings_one_client() RETURNS trigger AS $$
        DECLARE
          new_client BIGINT;
          other_client BIGINT;
        BEGIN
          -- Снятие или отключение привязки инвариант нарушить не может.
          IF NEW.status <> 'active' THEN
            RETURN NEW;
          END IF;

          SELECT client_id INTO new_client FROM projects WHERE id = NEW.project_id;
          SELECT p.client_id INTO other_client
            FROM chat_bindings b JOIN projects p ON p.id = b.project_id
           WHERE b.chat_ref = NEW.chat_ref
             AND b.id <> COALESCE(NEW.id, -1)
             AND b.status = 'active'
             AND p.client_id <> new_client
           LIMIT 1;

          IF other_client IS NOT NULL THEN
            RAISE EXCEPTION 'chat % already bound to another client (%), refusing %',
              NEW.chat_ref, other_client, new_client;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION chat_bindings_one_client() RETURNS trigger AS $$
        DECLARE
          new_client BIGINT;
          other_client BIGINT;
        BEGIN
          SELECT client_id INTO new_client FROM projects WHERE id = NEW.project_id;
          SELECT p.client_id INTO other_client
            FROM chat_bindings b JOIN projects p ON p.id = b.project_id
           WHERE b.chat_ref = NEW.chat_ref
             AND b.id <> COALESCE(NEW.id, -1)
             AND b.status = 'active'
             AND p.client_id <> new_client
           LIMIT 1;
          IF other_client IS NOT NULL THEN
            RAISE EXCEPTION 'chat % already bound to another client (%), refusing %',
              NEW.chat_ref, other_client, new_client;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
