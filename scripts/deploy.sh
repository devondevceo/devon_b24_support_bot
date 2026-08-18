#!/usr/bin/env bash
# Выкатка на боевой сервер. Вызывается из .github/workflows/deploy.yml по ssh,
# но обязан работать и руками — деплой, который умеет только CI, бесполезен в аварии.
#
# Порядок шагов — из docs/10-architecture.md §10. Каждая проверка здесь стоит
# отдельной аварии в журнале проекта, поэтому ни одну нельзя убрать «для скорости».
#
#   APP_DIR=/opt/b24sdbot IMAGE=ghcr.io/…:sha-abc123 GHCR_USER=<логин> \
#     bash scripts/deploy.sh   <<< "<токен реестра>"
#
# Токен реестра читается из stdin. Если stdin пуст, логин пропускается: образ,
# который уже лежит локально, выкатывается без обращения к реестру.

set -Eeuo pipefail

APP_DIR=${APP_DIR:-/opt/b24sdbot}
IMAGE=${IMAGE:?нужен IMAGE=ghcr.io/<владелец>/<репозиторий>:<тег>}
GHCR_USER=${GHCR_USER:-}
GIT_SHA=${GIT_SHA:-неизвестен}
SERVICES=(api bot worker)
ALL_CONTAINERS=(b24sdbot-postgres b24sdbot-api b24sdbot-bot b24sdbot-worker)
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-300}
DISK_MAX_PCT=${DISK_MAX_PCT:-85}
KEEP_DUMPS=${KEEP_DUMPS:-10}

REGISTRY=${IMAGE%%/*}
NETWORK=b24sdbot-internal

cd "$APP_DIR"

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "ОШИБКА: $*"; exit 1; }

# --- одна выкатка за раз ----------------------------------------------------
# 16.08 две сессии выкатывались одновременно и по очереди затирали друг друга.
exec 9>"$APP_DIR/.deploy.lock"
flock -n 9 || fail "выкатка уже идёт (.deploy.lock занят)"

switched=0
dump=""
prev_image=""
prev_head=""

cleanup() {
  local rc=$?
  [ -n "$GHCR_USER" ] && docker logout "$REGISTRY" >/dev/null 2>&1 || true
  if [ "$rc" -ne 0 ] && [ "$switched" -eq 1 ] && [ -n "$prev_image" ]; then
    log "———"
    log "ОТКАТ образа на $prev_image"
    if set_image "$prev_image" && docker compose up -d --no-build "${SERVICES[@]}"; then
      log "откат образа выполнен"
    else
      log "ОТКАТ НЕ УДАЛСЯ — сервис лежит, чинить руками"
    fi
    log "БАЗА НЕ ОТКАЧЕНА. Схема осталась новой; миграции вперёд-совместимы,"
    log "старый код с новой схемой работает. Ревизия до выкатки: ${prev_head:-неизвестна}"
    log "Полный откат с базой: scripts/rollback.sh --to $prev_image --with-db ${dump:-<дамп>}"
  fi
  exit "$rc"
}

# APP_IMAGE живёт в .env: тогда обычный `docker compose up -d`, набранный руками,
# поднимает ровно тот образ, что выкачен, а не случайный :latest.
set_image() {
  local ref=$1 tmp=.env.deploy-tmp
  [ -s .env ] || return 1
  umask 077
  { grep -v '^APP_IMAGE=' .env || true; } > "$tmp"
  printf 'APP_IMAGE=%s\n' "$ref" >> "$tmp"
  # .env без DATABASE_URL — это стёртые боевые секреты. Падаем ДО подмены файла.
  grep -q '^DATABASE_URL=' "$tmp" || { rm -f "$tmp"; return 1; }
  chmod 600 "$tmp"
  cp -p .env .env.prev-deploy
  mv "$tmp" .env
  export APP_IMAGE="$ref"
}

trap cleanup EXIT

log "выкатка $IMAGE (коммит $GIT_SHA)"

# --- 1. место на диске ------------------------------------------------------
used=$(df --output=pcent "$APP_DIR" | tail -1 | tr -dc '0-9')
[ "$used" -le "$DISK_MAX_PCT" ] || fail "на диске занято ${used}%, предел ${DISK_MAX_PCT}%"
log "диск: занято ${used}%"

[ -f .env ] || fail "нет $APP_DIR/.env"
[ -f docker-compose.yml ] || fail "нет $APP_DIR/docker-compose.yml"
# `docker run --env-file` кавычки не снимает, а compose снимает. Значение в кавычках
# доехало бы до alembic вместе с ними — и DATABASE_URL молча стал бы неверным.
if grep -qE '^[A-Za-z_][A-Za-z0-9_]*=["'"'"']' .env; then
  fail "в .env есть значения в кавычках — docker --env-file их не снимает"
fi

# Публичный адрес берём из .env сервера, а не из константы в скрипте: домен
# прописан в одном месте, и после переезда проверка не начнёт стучаться в старый.
if [ -z "${HEALTH_URL:-}" ]; then
  base=$(grep -m1 '^PUBLIC_BASE_URL=' .env | cut -d= -f2- || true)
  HEALTH_URL="${base:-https://b24sdbot.devondev.ru}/health"
fi

# --- 2. образ ---------------------------------------------------------------
if [ -n "$GHCR_USER" ] && [ ! -t 0 ]; then
  IFS= read -r ghcr_token || true
  [ -n "${ghcr_token:-}" ] || fail "токен реестра не пришёл на stdin"
  printf '%s' "$ghcr_token" | docker login "$REGISTRY" -u "$GHCR_USER" --password-stdin >/dev/null
  unset ghcr_token
  log "логин в $REGISTRY выполнен"
fi

docker pull "$IMAGE" >/dev/null || fail "не скачался образ $IMAGE"
new_digest=$(docker image inspect "$IMAGE" --format '{{.Id}}')
log "образ получен: ${new_digest:0:19}…"

# --- 3. что крутится сейчас (это и есть точка отката) -----------------------
prev_image=$(docker inspect b24sdbot-api --format '{{.Config.Image}}' 2>/dev/null || true)
[ -n "$prev_image" ] || prev_image=$(grep -m1 '^APP_IMAGE=' .env | cut -d= -f2- || true)
log "текущий образ: ${prev_image:-нет запущенного контейнера}"

# --- 4. дамп базы -----------------------------------------------------------
# Обязателен перед каждой выкаткой, а не только перед миграцией: цена — секунды
# и десяток мегабайт, цена его отсутствия — боевые данные пилота.
mkdir -p backups .deploy
dump="backups/pre-deploy-$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  | gzip > "$dump" || fail "pg_dump не отработал"
[ -s "$dump" ] || fail "дамп пустой: $dump"
# Оборванный дамп выглядит как нормальный файл — проверяем хвост, а не размер.
{ gzip -dc "$dump" || true; } | tail -3 | grep -q 'PostgreSQL database dump complete' \
  || fail "дамп оборван: $dump"
log "дамп снят: $dump ($(du -h "$dump" | cut -f1))"

# --- 5. миграции ------------------------------------------------------------
# Отдельным одноразовым контейнером НОВОГО образа, до подъёма сервисов: новый код
# с новой схемой обязан встретиться уже накатанным. Обратный порядок не гарантирован.
prev_head=$(docker run --rm --network "$NETWORK" --env-file .env "$IMAGE" \
              alembic current 2>/dev/null | tail -1 || true)
log "ревизия до миграции: ${prev_head:-неизвестна}"

docker run --rm --network "$NETWORK" --env-file .env "$IMAGE" alembic upgrade head \
  || fail "alembic upgrade head не прошёл (база не тронута дальше этой точки, дамп: $dump)"
log "миграции накатаны"

# --- 6. подъём --------------------------------------------------------------
set_image "$IMAGE" || fail "не удалось записать APP_IMAGE в .env"
switched=1
docker compose config -q || fail "docker-compose.yml не разбирается"
docker compose up -d --no-build "${SERVICES[@]}" || fail "docker compose up не отработал"

# --- 7. проверка, что крутится именно выкаченный образ ----------------------
# Прямая защита от аварии 16.08: контейнеры были healthy, а код в них — старый.
for svc in "${SERVICES[@]}"; do
  cid=$(docker compose ps -q "$svc")
  [ -n "$cid" ] || fail "сервис $svc не поднялся"
  running=$(docker inspect "$cid" --format '{{.Image}}')
  [ "$running" = "$new_digest" ] || fail "$svc работает не на выкаченном образе"
done
log "все сервисы на выкаченном образе"

# --- 8. здоровье ------------------------------------------------------------
deadline=$((SECONDS + HEALTH_TIMEOUT))
while :; do
  pending=()
  for c in "${ALL_CONTAINERS[@]}"; do
    state=$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo "нет")
    [ "$state" = "healthy" ] || pending+=("$c=$state")
  done
  [ ${#pending[@]} -eq 0 ] && break
  [ "$SECONDS" -lt "$deadline" ] || fail "не дождались healthy: ${pending[*]}"
  sleep 5
done
log "все четыре контейнера healthy"

curl -fsS --max-time 15 "$HEALTH_URL" | grep -q '"status":"ok"' \
  || fail "$HEALTH_URL не отвечает ok"
log "$HEALTH_URL отвечает ok"

# --- 9. уборка --------------------------------------------------------------
# image prune БЕЗ -a: с -a он снёс бы неиспользуемые образы соседей по машине.
docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
docker image prune -f --filter until=168h >/dev/null 2>&1 || true
ls -1t backups/pre-deploy-*.sql.gz 2>/dev/null | tail -n +$((KEEP_DUMPS + 1)) | xargs -r rm -f

printf '%s\t%s\t%s\t%s\n' "$(date -Is)" "$IMAGE" "$GIT_SHA" "${prev_image:-—}" >> .deploy/history
log "готово: $IMAGE"
