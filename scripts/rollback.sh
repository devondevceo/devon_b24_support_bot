#!/usr/bin/env bash
# Откат боевого сервера. Запускается руками на сервере — CI сюда не ходит.
#
# Выкатка (scripts/deploy.sh) при провале откатывает ТОЛЬКО образ: схема остаётся
# новой, потому что миграции вперёд-совместимы и старый код с новой схемой живёт.
# Этот скрипт — для случая, когда откатить надо больше.
#
#   scripts/rollback.sh                          — на предыдущий образ из .deploy/history
#   scripts/rollback.sh --to ghcr.io/…:sha-abc   — на конкретный образ
#   scripts/rollback.sh --migrations -1          — плюс alembic downgrade -1
#   scripts/rollback.sh --with-db backups/x.gz   — плюс восстановление базы из дампа
#
# --with-db уничтожает текущее содержимое базы. Спрашивает подтверждение,
# если не передан --yes.

set -Eeuo pipefail

APP_DIR=${APP_DIR:-/opt/b24sdbot}
SERVICES=(api bot worker)
NETWORK=b24sdbot-internal

target=""
migrations=""
dbdump=""
assume_yes=0

while [ $# -gt 0 ]; do
  case "$1" in
    --to)         target=${2:?нужен образ};        shift 2 ;;
    --migrations) migrations=${2:?нужна ревизия};   shift 2 ;;
    --with-db)    dbdump=${2:?нужен файл дампа};    shift 2 ;;
    --yes)        assume_yes=1;                     shift ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *)            echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
done

cd "$APP_DIR"

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "ОШИБКА: $*"; exit 1; }

exec 9>"$APP_DIR/.deploy.lock"
flock -n 9 || fail "выкатка или откат уже идёт (.deploy.lock занят)"

if [ -z "${HEALTH_URL:-}" ]; then
  base=$(grep -m1 '^PUBLIC_BASE_URL=' .env | cut -d= -f2- || true)
  HEALTH_URL="${base:-https://b24sdbot.devondev.ru}/health"
fi

# --- какой образ ------------------------------------------------------------
if [ -z "$target" ]; then
  # Четвёртая колонка журнала — образ, который работал ДО последней выкатки.
  target=$(tail -1 .deploy/history 2>/dev/null | cut -f4 || true)
  [ -n "$target" ] && [ "$target" != "—" ] \
    || fail "в .deploy/history нет предыдущего образа, укажите --to"
fi
docker image inspect "$target" >/dev/null 2>&1 \
  || fail "образа $target нет локально; docker pull $target (нужен логин в реестр)"
log "откат на $target"

# --- страховка перед откатом ------------------------------------------------
# Дамп снимается ДАЖЕ при откате: состояние «после аварии» — тоже данные,
# и восстановить его иначе будет неоткуда.
mkdir -p backups .deploy
safety="backups/pre-rollback-$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  | gzip > "$safety" || fail "pg_dump не отработал"
{ gzip -dc "$safety" || true; } | tail -3 | grep -q 'PostgreSQL database dump complete' \
  || fail "страховочный дамп оборван: $safety"
log "страховочный дамп: $safety"

# --- подтверждение разрушительного шага -------------------------------------
if [ -n "$dbdump" ] && [ "$assume_yes" -eq 0 ]; then
  [ -f "$dbdump" ] || fail "нет файла дампа: $dbdump"
  printf 'Восстановление из %s СОТРЁТ текущую базу. Продолжить? [yes/NO] ' "$dbdump"
  read -r answer
  [ "$answer" = "yes" ] || fail "отменено"
fi

# --- 1. образ ---------------------------------------------------------------
umask 077
{ grep -v '^APP_IMAGE=' .env || true; } > .env.rollback-tmp
printf 'APP_IMAGE=%s\n' "$target" >> .env.rollback-tmp
grep -q '^DATABASE_URL=' .env.rollback-tmp || fail ".env после правки потерял DATABASE_URL"
chmod 600 .env.rollback-tmp
cp -p .env .env.prev-deploy
mv .env.rollback-tmp .env
export APP_IMAGE="$target"

# --- 2. миграции ------------------------------------------------------------
if [ -n "$migrations" ]; then
  log "alembic downgrade $migrations"
  docker run --rm --network "$NETWORK" --env-file .env "$target" \
    alembic downgrade "$migrations" || fail "downgrade не прошёл, дамп: $safety"
fi

# --- 3. база ----------------------------------------------------------------
if [ -n "$dbdump" ]; then
  log "восстановление базы из $dbdump"
  docker compose stop "${SERVICES[@]}"
  docker compose exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
       -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\" WITH (FORCE)" \
       -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\""' \
    || fail "не удалось пересоздать базу"
  gzip -dc "$dbdump" | docker compose exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null \
    || fail "восстановление не прошло; исходное состояние в $safety"
  log "база восстановлена"
fi

# --- 4. подъём и проверка ---------------------------------------------------
docker compose up -d --no-build "${SERVICES[@]}"

deadline=$((SECONDS + 300))
while :; do
  pending=()
  for c in b24sdbot-postgres b24sdbot-api b24sdbot-bot b24sdbot-worker; do
    state=$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo "нет")
    [ "$state" = "healthy" ] || pending+=("$c=$state")
  done
  [ ${#pending[@]} -eq 0 ] && break
  [ "$SECONDS" -lt "$deadline" ] || fail "не дождались healthy: ${pending[*]}"
  sleep 5
done

curl -fsS --max-time 15 "$HEALTH_URL" | grep -q '"status":"ok"' \
  || fail "$HEALTH_URL не отвечает ok"

printf '%s\t%s\t%s\t%s\n' "$(date -Is)" "$target" "откат" "—" >> .deploy/history
log "откат выполнен: $target"
