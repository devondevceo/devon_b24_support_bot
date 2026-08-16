# Модель данных — PostgreSQL 16

**Этот документ — единственный источник правды по именам таблиц и колонок.**
Все остальные документы обязаны использовать имена отсюда. Если где-то встретилось другое
имя — это ошибка того документа, а не альтернатива.

## 0. Отвергнутые синонимы

Черновики называли одни и те же сущности по-разному. Канон слева, искать и заменять справа:

| Канон | Отвергнутые варианты |
|---|---|
| `outbox` | `tg_outbox` |
| `task_cache` | `b24_task_cache`, `tasks_cache` |
| `chat_bindings` | `chat_project_bindings`, `chat_scopes` |
| `survey_templates` / `survey_questions` / `survey_sessions` | `quest_categories`, `quest_steps`, `quest_sessions` |
| `tenant_members` | `tenant_users` |
| `b24_event_inbox` | `inbox_events`, `event_queue`, `tasks` |
| `sync_state` | `sync_cursors`, `project_sync_state` |
| `tg_chats` | `bot_chat_registrations` |
| `tg_fsm_states` | `fsm_state` |

Данные портала живут **в `tenants`**, отдельной таблицы `b24_portals` нет: теннант и портал
связаны 1:1 (решение заказчика), а резолв теннанта по `member_id` — горячий путь, лишний JOIN
там не нужен. Если когда-нибудь понадобится 1:N — портальные колонки выносятся отдельной
миграцией, код резолва при этом меняется в одном месте.

## 1. Общие соглашения

- Все идентификаторы — `BIGINT GENERATED ALWAYS AS IDENTITY`, кроме случаев, где ключ внешний.
- Все временные метки — `TIMESTAMPTZ`, хранение в UTC.
- **`tenant_id` обязателен во всех доменных таблицах** и участвует в каждом внешнем ключе.
  Исключений нет: `callback_tokens`, `outbox`, `tg_fsm_states` тоже его имеют, иначе удаление
  теннанта оставляет хвосты (найдено ревью).
- Идентификаторы Битрикса хранятся как `BIGINT` (API отдаёт строки — приводить в слое кэша).
- Telegram `chat_id` фигурирует **только** в `tg_chats.chat_id`. Все прочие таблицы ссылаются
  на суррогатный `tg_chats.id` — тогда миграция группы в супергруппу это один `UPDATE`.
- Шифрованные значения — домен `enc_text`, формат ниже.

### Шифрование

```
enc:2:<kid>:<base64url(nonce || ciphertext || tag)>
```

- AES-256-GCM. `kid` — версия мастер-ключа, мастер-ключ в переменной окружения.
- **AAD обязателен** и равен `table:column:tenant_id:natural_key`. Без него зашифрованный
  `refresh_token` можно скопировать из строки одного теннанта в строку другого, и он расшифруется.
- Рядом с каждой шифрованной колонкой — денормализованный `*_kid SMALLINT`, чтобы джоб ротации
  находил строки индексом, а не расшифровкой всей таблицы.

```sql
CREATE DOMAIN enc_text AS TEXT
  CHECK (VALUE ~ '^enc:[0-9]+:[0-9]+:[A-Za-z0-9_-]+$');
```

### Телефоны и почта

Сырые номера и адреса не хранятся. Только `bytea` от HMAC-SHA256 с отдельным pepper
(`PHONE_HMAC_PEPPER`, `EMAIL_HMAC_PEPPER`). Нормализация телефона — через библиотеку
`phonenumbers` в E.164 с явным регионом по умолчанию; при неоднозначной нормализации
сопоставление **не выполняется** (иначе десятизначный иностранный номер превращается
в российский и совпадает с чужим сотрудником).

---

## 2. Теннанты, порталы, боты

```sql
CREATE TABLE tenants (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug                TEXT        NOT NULL UNIQUE,
  name                TEXT        NOT NULL,
  status              TEXT        NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','suspended','uninstalled','deleted')),
  -- портал Битрикс24 (1:1)
  b24_member_id       TEXT        NOT NULL UNIQUE,   -- главный ключ резолва входящих запросов
  b24_domain          TEXT        NOT NULL,          -- devondev.bitrix24.ru
  b24_client_id       TEXT,                          -- не секрет
  b24_client_secret   enc_text,
  b24_client_secret_kid SMALLINT,
  b24_app_token       enc_text,                      -- APPLICATION_TOKEN, меняется при ONAPPUPDATE
  b24_app_token_kid   SMALLINT,
  granted_scope       TEXT[]      NOT NULL DEFAULT '{}',
  install_state       TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (install_state IN ('pending','installed','finished','broken')),
  tz                  TEXT        NOT NULL DEFAULT 'Europe/Moscow',
  audit_mode          TEXT        NOT NULL DEFAULT 'full' CHECK (audit_mode IN ('full','minimal')),
  audit_mode_effective_at TIMESTAMPTZ,               -- переключение вступает в силу через 24 ч
  settings            JSONB       NOT NULL DEFAULT '{}',
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN tenants.audit_mode_effective_at IS
  'Отложенное применение minimal: точечное сокрытие действий становится бесполезным';
COMMENT ON COLUMN tenants.b24_app_token IS
  'НЕ доказательство подлинности события: значение видит любой сотрудник портала через F12';
```

> `oauth_host` намеренно **не** хранится: хост берётся из жёсткого списка
> `{oauth.bitrix24.tech, oauth.bitrix.info}`, `client_endpoint` собирается как
> `https://<b24_domain>/rest/`. Значения из входящих запросов (`SERVER_ENDPOINT` в
> placement-POST формирует браузер пользователя) не сохраняются никогда — это SSRF.

```sql
CREATE TABLE tg_bots (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id       BIGINT      NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
  bot_id          BIGINT      NOT NULL,        -- числовая часть токена, от getMe
  username        TEXT        NOT NULL,
  token           enc_text    NOT NULL,
  token_kid       SMALLINT    NOT NULL,
  webhook_id      UUID        NOT NULL DEFAULT gen_random_uuid(),
  webhook_secret  enc_text    NOT NULL,
  webhook_secret_kid SMALLINT NOT NULL,
  mode            TEXT        NOT NULL DEFAULT 'webhook' CHECK (mode IN ('webhook','polling')),
  proxy_url       TEXT,                         -- если api.telegram.org недоступен напрямую
  status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','active','error','suspended')),
  last_check_at   TIMESTAMPTZ,
  last_error      TEXT,
  -- короткое имя мини-аппа из BotFather (`/newapp`), миграция 0010.
  -- NULL = не заведено: из группы мини-апп открывается через личку бота.
  miniapp_short_name TEXT
                    CHECK (miniapp_short_name IS NULL
                           OR miniapp_short_name ~ '^[A-Za-z][A-Za-z0-9_]{2,29}$'),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_tg_bots__bot_id ON tg_bots (bot_id);
CREATE UNIQUE INDEX ux_tg_bots__webhook_id ON tg_bots (webhook_id);
```

> **`UNIQUE(bot_id)` глобальный, а не в пределах теннанта.** Без него админ теннанта B,
> заполучив токен бота теннанта A, вставляет его себе, наш reconcile делает `setWebhook`
> на свой адрес — и вся переписка чатов клиентов A уходит к B, а у A бот молча перестаёт
> работать. При вводе токена обязателен `getMe` и проверка, не занят ли `bot_id`.

## 3. Клиенты, проекты, стадии

```sql
CREATE TABLE clients (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name           TEXT        NOT NULL,
  crm_company_id BIGINT,                       -- компания в CRM портала теннанта
  status         TEXT        NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','archived')),
  tz             TEXT,                          -- NULL = наследовать от теннанта
  settings       JSONB       NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, name)
);

CREATE TABLE projects (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  client_id         BIGINT      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  b24_group_id      BIGINT      NOT NULL,
  kind              TEXT        NOT NULL DEFAULT 'workgroup'
                      CHECK (kind IN ('workgroup','project','collab')),
  name              TEXT        NOT NULL,
  is_extranet       BOOLEAN     NOT NULL DEFAULT false,
  owner_b24_user_id BIGINT,
  status            TEXT        NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','archived','deleted')),
  name_synced_at    TIMESTAMPTZ,
  members_synced_at TIMESTAMPTZ,
  stages_synced_at  TIMESTAMPTZ,
  defaults          JSONB       NOT NULL DEFAULT '{}',  -- дефолты полей задачи
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, b24_group_id)
);
COMMENT ON COLUMN projects.kind IS
  'На devondev все 15 групп — workgroup с PROJECT=Y. collab оставлен на случай других порталов';
COMMENT ON COLUMN projects.status IS
  'archived/deleted проставляет суточный джоб sync_projects: группа переименована, закрыта или удалена';

-- Стадии канбана. Разведка показала: "Новые" — это стадия, а не статус,
-- и у каждого проекта стадии свои. Источник: task.stages.get(entityId=<b24_group_id>)
CREATE TABLE project_stages (
  tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  project_id   BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  b24_stage_id BIGINT      NOT NULL,
  title        TEXT        NOT NULL,
  sort         INT         NOT NULL DEFAULT 0,
  system_type  TEXT,                            -- 'NEW' у первой колонки
  color        TEXT,
  synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, project_id, b24_stage_id)
);
```

## 4. Чаты, топики, привязки

```sql
CREATE TABLE tg_chats (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- суррогатный, ссылаться на него
  chat_id       BIGINT      NOT NULL,           -- телеграмный, меняется при миграции в супергруппу
  tenant_id     BIGINT      REFERENCES tenants(id) ON DELETE CASCADE,  -- NULL = unclaimed
  bot_id        BIGINT      REFERENCES tg_bots(id) ON DELETE SET NULL,
  type          TEXT        NOT NULL,
  title         TEXT,
  is_forum      BOOLEAN     NOT NULL DEFAULT false,
  status        TEXT        NOT NULL DEFAULT 'unclaimed'
                  CHECK (status IN ('unclaimed','claimed','active','left','migrated')),
  migrated_to   BIGINT REFERENCES tg_chats(id),
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_tg_chats__chat_id ON tg_chats (chat_id) WHERE status <> 'migrated';
```

> Строка создаётся **только** из фактического апдейта Telegram (`my_chat_member` или
> сообщение). Ручной ввод `chat_id` в панели запрещён: иначе теннант B занимает чат клиента
> теннанта A и навсегда блокирует привязку, а по тексту ошибки узнаёт, обслуживается ли
> этот чат конкурентом через наш сервис. Ошибка конфликта — обезличенная.

```sql
CREATE TABLE tg_topics (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  chat_ref       BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
  thread_id      BIGINT      NOT NULL,
  name           TEXT,
  is_closed      BOOLEAN     NOT NULL DEFAULT false,
  is_deleted     BOOLEAN     NOT NULL DEFAULT false,
  name_updated_at TIMESTAMPTZ,
  UNIQUE (chat_ref, thread_id)
);

CREATE TABLE chat_bindings (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  chat_ref    BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
  topic_ref   BIGINT      REFERENCES tg_topics(id) ON DELETE CASCADE,  -- NULL = весь чат
  project_id  BIGINT      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status      TEXT        NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','broken','disabled')),
  created_by  BIGINT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_chat_bindings__uniq
  ON chat_bindings (chat_ref, COALESCE(topic_ref, 0), project_id);
CREATE INDEX ix_chat_bindings__project ON chat_bindings (tenant_id, project_id)
  WHERE status = 'active';
```

**Инвариант, проверяемый триггером и тестом:** все проекты, привязанные к одному чату,
принадлежат одному `client_id`. Это прямое требование заказчика и одновременно граница
изоляции между клиентами.

## 5. Люди, роли, токены

```sql
CREATE TABLE users (                       -- глобальная запись человека
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tg_user_id    BIGINT UNIQUE,
  tg_username   TEXT,                      -- показываем, но НИКОГДА не используем как ключ
  display_name  TEXT,
  is_superadmin BOOLEAN     NOT NULL DEFAULT false,
  session_epoch INT         NOT NULL DEFAULT 1,   -- инкремент = отзыв всех сессий
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenant_members (
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role         TEXT   NOT NULL CHECK (role IN ('tenant_admin','member')),
  b24_user_id  BIGINT,
  link_status  TEXT   NOT NULL DEFAULT 'none'
                 CHECK (link_status IN ('none','matched','authorized','needs_reauth','revoked')),
  linked_at    TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, user_id)
);
```

- `matched` — мы знаем, кто это в Битриксе, но токена нет: чтение и подсказки.
- `authorized` — есть живой per-user токен: создание и редактирование задач.

```sql
CREATE TABLE b24_user_tokens (
  tenant_id             BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  b24_user_id           BIGINT      NOT NULL,
  role                  TEXT        NOT NULL DEFAULT 'user'
                          CHECK (role IN ('user','service_admin','service_reader')),
  authorized_tg_user_id BIGINT,     -- КТО авторизовал этот токен
  access_token          enc_text    NOT NULL,
  refresh_token         enc_text    NOT NULL,
  enc_kid               SMALLINT    NOT NULL,
  expires_at            TIMESTAMPTZ NOT NULL,
  token_version         INT         NOT NULL DEFAULT 1,
  state                 TEXT        NOT NULL DEFAULT 'active'
                          CHECK (state IN ('active','needs_reauth','revoked')),
  last_refresh_at       TIMESTAMPTZ,
  last_used_at          TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, b24_user_id)
);
CREATE INDEX ix_b24_user_tokens__warmup ON b24_user_tokens (last_refresh_at)
  WHERE state = 'active';
CREATE INDEX ix_b24_user_tokens__rekey ON b24_user_tokens (enc_kid);
```

> **`authorized_tg_user_id` — критическая колонка.** Ревью нашло дыру: привязка по e-mail
> без подтверждения владения давала атакующему доступ к токену жертвы, потому что токен
> искался по `(tenant_id, b24_user_id)`. Правило: `UserTokenSource` отдаёт токен только если
> `authorized_tg_user_id` совпадает с тем, кто сейчас действует. Иначе — отказ и требование
> пройти placement самому. Ручное сопоставление админом даёт максимум `matched`.

```sql
CREATE TABLE b24_users_cache (
  tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  b24_user_id   BIGINT NOT NULL,
  name          TEXT,
  last_name     TEXT,
  work_position TEXT,
  user_type     TEXT,                       -- employee | extranet
  active        BOOLEAN NOT NULL DEFAULT true,
  email_hash    BYTEA,
  phone_hash    BYTEA,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, b24_user_id)
);
CREATE INDEX ix_b24_users_cache__phone ON b24_users_cache (tenant_id, phone_hash)
  WHERE phone_hash IS NOT NULL;
CREATE INDEX ix_b24_users_cache__email ON b24_users_cache (tenant_id, email_hash)
  WHERE email_hash IS NOT NULL;
```

## 6. Кэш задач

```sql
CREATE TABLE task_cache (
  tenant_id       BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  b24_task_id     BIGINT      NOT NULL,
  project_id      BIGINT      REFERENCES projects(id) ON DELETE SET NULL,
  b24_group_id    BIGINT,
  is_ours         BOOLEAN     NOT NULL DEFAULT true,
  title           TEXT,
  status          SMALLINT,                  -- REAL_STATUS: 2..6
  sub_status      SMALLINT,                  -- STATUS: те же + псевдо -1/-2/-3
  stage_id        BIGINT,
  responsible_id  BIGINT,
  created_by      BIGINT,
  accomplices     BIGINT[],
  auditors        BIGINT[],
  priority        SMALLINT,
  deadline        TIMESTAMPTZ,
  created_date    TIMESTAMPTZ,
  changed_date    TIMESTAMPTZ,
  closed_date     TIMESTAMPTZ,
  comments_count  INT,
  overdue_notified_at TIMESTAMPTZ,
  synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ,               -- только для отбойников
  PRIMARY KEY (tenant_id, b24_task_id)
);
CREATE INDEX ix_task_cache__project_open ON task_cache (tenant_id, project_id, status)
  WHERE is_ours AND status <> 5;
CREATE INDEX ix_task_cache__overdue ON task_cache (tenant_id, project_id, deadline)
  WHERE is_ours AND status <> 5 AND deadline IS NOT NULL;
CREATE INDEX ix_task_cache__responsible ON task_cache (tenant_id, responsible_id)
  WHERE is_ours AND status <> 5;
CREATE INDEX ix_task_cache__route ON task_cache (tenant_id, b24_group_id);
ALTER TABLE task_cache SET (fillfactor = 85, autovacuum_vacuum_scale_factor = 0.05);
```

> **`is_ours = false` — это «отбойник».** Подписка на события Битрикса не фильтруется по
> группе: мы получаем события **всех** задач портала. Каждое неизвестное `task_id` требует
> дозапроса только чтобы узнать `GROUP_ID` и выбросить событие. Без отрицательного кэша мы
> тратим большую часть квоты портала на дозапрос чужих задач и ломаем другие интеграции
> теннанта. Строка-отбойник живёт 7 дней (`expires_at`).

```sql
CREATE TABLE task_change_log (      -- наблюдаемые изменения: основа SLA и разбора споров
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  b24_task_id  BIGINT      NOT NULL,
  field        TEXT        NOT NULL,          -- STATUS | STAGE | RESPONSIBLE | COMMENT | ...
  old_value    TEXT,
  new_value    TEXT,
  b24_user_id  BIGINT,
  source       TEXT        NOT NULL CHECK (source IN ('bot','event','history','sync')),
  changed_at   TIMESTAMPTZ NOT NULL,
  UNIQUE (tenant_id, b24_task_id, field, changed_at, source)
);
```

> Источник `history` — `tasks.task.history.list`, который отдаёт `{field, value:{from,to}, user}`.
> Разведка подтвердила, что он работает: «первая реакция» для SLA вычислима, а не выдумана.

## 7. Очередь и события

**Одна модель, а не две.** Специализированные `outbox` и `b24_event_inbox`, без универсальной
таблицы задач. Причина: у них разные ключи дедупликации, разные лимитеры и разные политики
ретраев, а универсальная таблица провоцирует класть в неё сырые апдейты Telegram.

```sql
CREATE TABLE outbox (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  bot_id         BIGINT      NOT NULL REFERENCES tg_bots(id) ON DELETE CASCADE,
  chat_ref       BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
  thread_id      BIGINT,
  kind           TEXT        NOT NULL,        -- notify | card | report | system
  payload        JSONB       NOT NULL,        -- ССЫЛКИ и КОДЫ, не тексты сообщений
  priority       SMALLINT    NOT NULL DEFAULT 100,
  dedup_key      TEXT,
  state          TEXT        NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','sending','sent','failed','cancelled')),
  attempts       SMALLINT    NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox__ready ON outbox (next_attempt_at, priority)
  WHERE state = 'pending';
CREATE UNIQUE INDEX ux_outbox__dedup ON outbox (tenant_id, dedup_key)
  WHERE dedup_key IS NOT NULL AND state IN ('pending','sending');
```

> **Инвариант И-1: сырой `Update` Telegram не попадает в БД.** Privacy mode выключен, значит
> в очередь легла бы вся переписка сотрудников клиента — в таблицу, в WAL и в бэкапы с
> ретенцией недели. Апдейт обрабатывается синхронно в процессе бота; в очередь идут только
> производные команды с идентификаторами. Проверяется канареечным тестом: уникальная строка
> в сообщении не должна находиться полнотекстовым поиском ни в одной таблице.

```sql
CREATE TABLE b24_event_inbox (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event        TEXT        NOT NULL,
  b24_task_id  BIGINT,
  dedup_key    TEXT        NOT NULL,
  state        TEXT        NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending','processing','done','failed','dropped')),
  attempts     SMALLINT    NOT NULL DEFAULT 0,
  received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  UNIQUE (tenant_id, dedup_key)
);
CREATE INDEX ix_event_inbox__pending ON b24_event_inbox (received_at) WHERE state = 'pending';
```

> `dedup_key` считается **из данных дозапроса**, а не из тела события. Тело события подделывается
> любым сотрудником портала (`APPLICATION_TOKEN` виден в F12), и вставка заранее подготовленного
> ключа погасила бы настоящее событие.

```sql
CREATE TABLE b24_echo_suppress (
  tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  b24_task_id BIGINT      NOT NULL,
  fingerprint TEXT        NOT NULL,   -- sha256(field | new_value | b24_user_id)
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, b24_task_id, fingerprint)
);
```

> Fingerprint включает **новое значение и автора**, гасит однократно, TTL 60 секунд, без слияния
> наборов полей. Прежняя схема со слиянием позволяла держать задачу «немой» в чате сколь угодно
> долго, повторяя безобидную правку раз в минуту.

```sql
CREATE TABLE b24_portal_budget (      -- лимитер: 2 rps (ведро 50) + operating 420 с / 10 мин
  tenant_id        BIGINT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  bucket_tokens    REAL        NOT NULL DEFAULT 50,
  bucket_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  operating_spent  REAL        NOT NULL DEFAULT 0,
  operating_reset_at TIMESTAMPTZ,
  throttled_until  TIMESTAMPTZ
);

CREATE TABLE tg_chat_budget (
  chat_ref      BIGINT PRIMARY KEY REFERENCES tg_chats(id) ON DELETE CASCADE,
  minute_window TIMESTAMPTZ NOT NULL,
  sent_in_window SMALLINT   NOT NULL DEFAULT 0,
  throttled_until TIMESTAMPTZ
);
```

## 8. Связи сообщений, идемпотентность, кнопки

```sql
CREATE TABLE tg_message_links (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  chat_ref     BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
  message_id   BIGINT      NOT NULL,
  kind         TEXT        NOT NULL CHECK (kind IN ('source','card','draft','notify')),
  b24_task_id  BIGINT,
  render_hash  TEXT,                    -- защита от "message is not modified"
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (chat_ref, message_id, kind)
);

CREATE TABLE entity_external_refs (     -- идемпотентность мутаций в Б24
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  idem_key    TEXT        NOT NULL,     -- draft_token, уходит в задачу маркером
  source_kind TEXT        NOT NULL,     -- tg_message | survey_session | attachment
  source_key  TEXT        NOT NULL,
  target_kind TEXT        NOT NULL,     -- b24_task | b24_comment | b24_file
  target_id   BIGINT,
  state       TEXT        NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','committed','failed')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idem_key)
);
CREATE INDEX ix_ext_refs__source ON entity_external_refs (tenant_id, source_kind, source_key);
```

> Закрывает самый опасный класс багов: `tasks.task.add` ушёл, портал задачу создал, ответ не
> дошёл по таймауту. При любом ретрае сначала ищем уже созданную задачу по `idem_key`
> (маркер в задаче), и только не найдя — создаём. Без этого автоповтор гарантированно
> плодит дубли. То же для комментариев и вложений: `state='committed'` никогда не перезаливается.

```sql
CREATE TABLE callback_tokens (
  token_hash  BYTEA       PRIMARY KEY,          -- sha256 от 16 случайных байт
  tenant_id   BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind        TEXT        NOT NULL,
  owner_tg_id BIGINT,
  chat_ref    BIGINT      REFERENCES tg_chats(id) ON DELETE CASCADE,
  payload     JSONB       NOT NULL DEFAULT '{}',
  single_use  BOOLEAN     NOT NULL DEFAULT true,
  used_at     TIMESTAMPTZ,
  expires_at  TIMESTAMPTZ NOT NULL,
  CONSTRAINT ck_admin_owner CHECK (kind NOT LIKE 'admin:%' OR owner_tg_id IS NOT NULL)
);
CREATE INDEX ix_callback_tokens__gc ON callback_tokens (expires_at);
```

> Прежний вариант `CHAR(8)` от `bigserial` был предсказуем: узнав свой токен `N`, атакующий
> перебирал `N±k` и завершал чужую привязку чата. Наружу уходит случайное значение, в БД
> лежит его хеш, сравнение constant-time.

## 9. Настройки с наследованием

```sql
CREATE TABLE notification_settings (
  tenant_id  BIGINT  NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  scope_kind TEXT    NOT NULL CHECK (scope_kind IN ('tenant','project','binding')),
  scope_id   BIGINT  NOT NULL,          -- 0 для tenant
  code       TEXT    NOT NULL,          -- task.status_changed, task.comment_added, ...
  enabled    BOOLEAN NOT NULL,
  PRIMARY KEY (tenant_id, scope_kind, scope_id, code)
);
```

**Семантика разрешения, единая для всех настроек проекта:**
`binding` → `project` → `tenant` → системный дефолт.
**Отсутствие записи = наследовать выше. Запись = явное решение.** UI обязан писать явный `false`,
а не удалять строку. В `mclick` отсутствие ключа трактовалось как «включено» — это стоило инцидента.

Та же семантика у `task_defaults` (дефолты полей задачи): `projects.defaults` перекрывает
`tenants.settings->'task_defaults'`, отсутствие ключа = наследовать, `null` = явный сброс.

## 10. Опросник

```sql
CREATE TABLE survey_templates (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  BIGINT REFERENCES tenants(id) ON DELETE CASCADE,   -- NULL = системный
  code       TEXT   NOT NULL,
  title      TEXT   NOT NULL,
  is_active  BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (tenant_id, code)
);

CREATE TABLE survey_questions (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   BIGINT  REFERENCES tenants(id) ON DELETE CASCADE,  -- NULL = системный
  template_id BIGINT  NOT NULL REFERENCES survey_templates(id) ON DELETE CASCADE,
  sort        INT     NOT NULL,
  code        TEXT    NOT NULL,
  text        TEXT    NOT NULL,
  answer_kind TEXT    NOT NULL CHECK (answer_kind IN ('text','choice','file','skip')),
  options     JSONB,
  required    BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX ix_survey_questions__tenant
  ON survey_questions (tenant_id, template_id, sort);
-- Триггер trg_survey_questions_tenant сверяет tenant_id вопроса с шаблоном:
-- CHECK здесь невозможен, условие смотрит в соседнюю таблицу.

CREATE TABLE survey_sessions (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    BIGINT      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  chat_ref     BIGINT      NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
  thread_id    BIGINT,
  owner_tg_id  BIGINT      NOT NULL,
  template_id  BIGINT      NOT NULL REFERENCES survey_templates(id),
  project_id   BIGINT      REFERENCES projects(id),
  step         INT         NOT NULL DEFAULT 0,
  answers      JSONB       NOT NULL DEFAULT '{}',
  state        TEXT        NOT NULL DEFAULT 'active'
                 CHECK (state IN ('active','done','expired','cancelled')),
  expires_at   TIMESTAMPTZ NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_survey_sessions__active ON survey_sessions (chat_ref, owner_tg_id)
  WHERE state = 'active';
```

> Ответы принимаются **только реплаем** на сообщение бота. Приём «первого некомандного
> сообщения владельца сессии в течение 120 секунд» в общем чате съедал бы обычные реплики
> коллегам («ага», «щас гляну») и отправлял их в описание задачи в Битриксе.

> `tenant_id` у вопроса появился миграцией `0009` — до неё вопрос ссылался только на шаблон,
> и выборка шла по одному `template_id`, то есть изоляция держалась на дисциплине
> вызывающего. Нашёл тест-страж `test_every_domain_table_carries_tenant_id`; это ровно тот
> случай, ради которого он написан. `survey.questions()` теперь требует `tenant_id`
> и берёт `tenant_id = $2 OR tenant_id IS NULL` — своё плюс системное.

## 11. Журналы

```sql
CREATE TABLE audit_log (            -- партиционируется по месяцам
  id         BIGINT GENERATED ALWAYS AS IDENTITY,
  tenant_id  BIGINT      NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_kind TEXT        NOT NULL,   -- user | system | b24
  actor_id   BIGINT,
  action     TEXT        NOT NULL,
  client_id  BIGINT,
  project_id BIGINT,
  target     TEXT,
  detail     JSONB,
  high_risk  BOOLEAN     NOT NULL DEFAULT false,
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE security_log (LIKE audit_log INCLUDING ALL);   -- НЕотключаемый
CREATE TABLE pd_access_log (LIKE audit_log INCLUDING ALL);  -- НЕотключаемый, доступ к ПДн
```

**Правила аудита:**
- `audit_mode='minimal'` вырезает только `detail`, но не факт события.
- Переключение в `minimal` вступает в силу **через 24 часа** (`audit_mode_effective_at`),
  иначе админ переключает режим, делает действие и возвращает обратно — и значения полей
  «до/после» исчезают навсегда.
- `high_risk = true` пишется с полным `detail` **всегда**, независимо от режима: смена
  ответственного, закрытие задачи, привязка и отвязка чата, ручное сопоставление,
  смена токена бота, смена сервисного пользователя, экспорт отчётов.
- Уменьшение ретенции — только суперадмином и только «вперёд».

## 12. Прочее

```sql
CREATE TABLE messages (             -- каталог системных текстов с переопределением теннантом
  tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE,  -- NULL = системный
  code      TEXT NOT NULL,
  lang      TEXT NOT NULL DEFAULT 'ru',
  text      TEXT NOT NULL,
  PRIMARY KEY (COALESCE(tenant_id, 0), code, lang)
);

CREATE TABLE bind_requests (        -- заявки на ручную привязку
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  hint       TEXT,
  state      TEXT   NOT NULL DEFAULT 'pending'
               CHECK (state IN ('pending','approved','rejected','expired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE scheduled_reports (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  binding_id   BIGINT NOT NULL REFERENCES chat_bindings(id) ON DELETE CASCADE,
  kind         TEXT   NOT NULL,
  send_at_local TIME  NOT NULL,
  tz           TEXT   NOT NULL,          -- явная колонка, не наследуется на лету
  days         SMALLINT[] NOT NULL DEFAULT '{1,2,3,4,5}',
  next_run_at  TIMESTAMPTZ NOT NULL,
  is_active    BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE sync_state (
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  scope       TEXT   NOT NULL,           -- tasks:<project_id> | projects | users | stages
  cursor      JSONB  NOT NULL DEFAULT '{}',
  phase       TEXT   NOT NULL DEFAULT 'initial' CHECK (phase IN ('initial','steady')),
  last_ok_at  TIMESTAMPTZ,
  consecutive_errors SMALLINT NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, scope)
);

CREATE TABLE job_locks (
  name       TEXT PRIMARY KEY,
  locked_by  TEXT,
  locked_at  TIMESTAMPTZ,
  expires_at TIMESTAMPTZ
);

CREATE TABLE tg_fsm_states (
  tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  chat_ref   BIGINT NOT NULL REFERENCES tg_chats(id) ON DELETE CASCADE,
  tg_user_id BIGINT NOT NULL,
  thread_id  BIGINT NOT NULL DEFAULT 0,
  state      TEXT,
  data       JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chat_ref, tg_user_id, thread_id)
);

CREATE TABLE tg_attachments (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  idem_key    TEXT   NOT NULL,
  tg_file_id  TEXT   NOT NULL,
  file_name   TEXT,                       -- нормализованное: без bidi, ≤100 символов
  size_bytes  BIGINT,
  state       TEXT   NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','uploaded','failed','rejected')),
  b24_file_id BIGINT,
  error       TEXT,
  UNIQUE (tenant_id, idem_key, tg_file_id)
);

CREATE TABLE web_sessions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tenant_id     BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
  session_epoch INT    NOT NULL,          -- сверяется с users.session_epoch
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL
);
```

**Вырезаемые вместе с функцией:** `sla_policies`, `task_sla_state`, `task_time_entries` —
если SLA и отчёт по времени уходят из фазы 1 (см. [70-plan.md](70-plan.md) §5).

## 13. Изоляция

Три уровня защиты, потому что одного мало:

1. **Обязательный `tenant_id`** в каждом запросе. Тест-страж: список таблиц из миграций
   сверяется со списком таблиц, имеющих колонку `tenant_id`; расхождение валит сборку.
2. **RLS** на всех доменных таблицах, роль приложения `b24bot_app` без `BYPASSRLS`,
   `SET LOCAL app.tenant_id` в начале каждой транзакции.
3. **Резолв теннанта — `SECURITY DEFINER` функции**, а не роль с `BYPASSRLS`:

```sql
CREATE FUNCTION resolve_tenant_by_member_id(p_member_id TEXT)
  RETURNS TABLE (tenant_id BIGINT, status TEXT) SECURITY DEFINER AS $$
  SELECT id, status FROM tenants WHERE b24_member_id = p_member_id;
$$ LANGUAGE sql STABLE;

CREATE FUNCTION resolve_bot_by_webhook_id(p_webhook_id UUID)
  RETURNS TABLE (bot_pk BIGINT, tenant_id BIGINT, status TEXT) SECURITY DEFINER AS $$
  SELECT id, tenant_id, status FROM tg_bots WHERE webhook_id = p_webhook_id;
$$ LANGUAGE sql STABLE;
```

Горячий путь принимает недоверенные данные первым; роль с `BYPASSRLS` там обходила бы всю
защиту разом, включая тесты изоляции, которые проверяют только обычный путь.
`b24bot_admin` с `BYPASSRLS` остаётся только для миграций, его использование пишется в `security_log`.

**Отдельный инвариант:** все проекты, привязанные к одному чату, принадлежат одному клиенту.
Проверяется триггером на `chat_bindings` и отдельным тестом.

### 13.1 Что из этого сделано на 16.08.2026

| Уровень | Состояние |
|---|---|
| 1. Обязательный `tenant_id` | ✅ страж написан и нашёл первую дыру (`survey_questions`, миграция `0009`) |
| 2. RLS | ❌ не включён: приложение до сих пор ходит владельцем базы. Изоляцию сейчас держит только код |
| 3. `SECURITY DEFINER`-резолверы | ❌ нет, резолв идёт обычными запросами |

Тесты изоляции — `tests/test_isolation.py`, пять штук, требуют настоящей PostgreSQL:

```bash
docker run --rm --network b24sdbot-internal -v /opt/b24sdbot:/w -w /tmp \
  -e TEST_DATABASE_URL="postgresql://<user>:<pass>@b24sdbot-postgres:5432/postgres" \
  -e PYTHONPATH=/w/src b24sdbot-api:latest \
  sh -lc 'cp -r /w/tests /w/migrations /w/alembic.ini /w/pyproject.toml /tmp/; ln -s /w/src /tmp/src; pip install -q pytest pytest-asyncio alembic; python -m pytest tests/test_isolation.py -q -p no:cacheprovider'
```

Модуль поднимает **отдельную базу**, накатывает всю цепочку миграций, откатывает её до
`base` и накатывает снова — то есть обратная совместимость проверяется на каждом прогоне.
Без `TEST_DATABASE_URL` тесты пропускаются, чтобы прогон без базы не краснел.

**Список исключений из И-2** (таблицы без `tenant_id`, каждая с причиной — список живёт
в самом тесте, чтобы новая таблица не проскочила молча):

| Таблица | Почему без `tenant_id` |
|---|---|
| `tenants` | сам теннант, его ключ — `id` |
| `users` | глобальная запись человека: один Telegram-аккаунт может состоять в нескольких теннантах, привязка лежит в `tenant_members` |
| `b24_payload_log` | журнал **структуры** входящих payload, пишется до того, как теннант определён (событие install). Таблица спайковая, удаляется отдельной миграцией |
| `alembic_version` | служебная |

## 14. Ретенция

| Таблица | Срок | Чем чистится |
|---|---|---|
| `audit_log` | 365 дней | дроп партиции |
| `security_log`, `pd_access_log` | 365 дней, уменьшение только суперадмином | дроп партиции |
| `task_change_log` | 400 дней | джоб |
| `outbox` (`sent`/`cancelled`) | 7 дней | джоб |
| `b24_event_inbox` (`done`/`dropped`) | 14 дней | джоб |
| `task_cache` отбойники (`is_ours=false`) | 7 дней | по `expires_at` |
| `task_cache` закрытые задачи | 90 дней после `closed_date` | джоб |
| `callback_tokens` | по `expires_at` + 1 день | джоб раз в час |
| `survey_sessions`, `tg_fsm_states` | 30 дней | джоб |
| `tg_message_links` | 180 дней | джоб |
| `web_sessions` | по `expires_at` | джоб |

## 15. Объём и профиль PostgreSQL

Расчёт на теннанта за первый год (20 клиентов, 5000 задач): **~130 МБ**, с bloat ~160 МБ.
20 теннантов — ~3.2 ГБ данных, диск под БД ~8 ГБ с WAL и служебным.

Горячий рабочий набор — ~6 МБ на теннанта, то есть 20 теннантов ≈ 120 МБ страниц.

**Единый профиль** (он же в `docker-compose.yml`, второго профиля не существует):

```yaml
postgres:
  image: postgres:16-alpine
  shm_size: 128mb
  mem_limit: 1200m
  memswap_limit: 1600m        # 400 МБ свопа: плохо, но лучше, чем PANIC и recovery
  cpu_shares: 512             # проигрывать соседям по VPS при конкуренции
  # postgres требует "-c" и значение РАЗНЫМИ аргументами.
  # Форма "-c=key=value" даёт FATAL: unrecognized configuration parameter ""
  # и контейнер уходит в цикл перезапусков. Проверено на боевом сервере 15.08.2026.
  command:
    - postgres
    - -c
    - max_connections=40
    - -c
    - shared_buffers=256MB
    - -c
    - effective_cache_size=768MB
    - -c
    - work_mem=4MB
    - -c
    - maintenance_work_mem=64MB
    - -c
    - autovacuum_max_workers=2
    - -c
    - autovacuum_work_mem=48MB
    - -c
    - autovacuum_naptime=30s
    - -c
    - wal_compression=on
    - -c
    - max_wal_size=1GB
    - -c
    - checkpoint_completion_target=0.9
    - -c
    - random_page_cost=1.1
    - -c
    - max_parallel_workers_per_gather=1
    - -c
    - shared_preload_libraries=pg_stat_statements
```

Арифметика худшего случая: `256` (shared) + `40×4×2 = 320` (work_mem) + `2×48 = 96`
(autovacuum) + `40×8 = 320` (приватная память бэкендов) + `~30` прочее = **~1020 МБ**
при лимите 1200 МБ. Сходится с запасом 15%. Тяжёлым отчётным запросам `work_mem`
поднимается точечно через `SET LOCAL`, а не глобально.

**Пулы соединений считаются как `workers × max_size`** (в черновике эта ошибка давала
занижение в 1.5 раза): api 1 воркер × 8 + bot 8 + worker 10 + 2 отдельных соединения под
`LISTEN` мимо пула = **28 из 40 (70%)**. Число воркеров api вынесено в переменную окружения
с комментарием-формулой, чтобы его нельзя было поднять, не увидев расчёт.
