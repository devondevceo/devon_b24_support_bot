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
PUBLIC_TIMEOUT=${PUBLIC_TIMEOUT:-90}   # сколько ждём, пока Traefik переключит маршрут
DISK_MAX_PCT=${DISK_MAX_PCT:-85}
KEEP_DUMPS=${KEEP_DUMPS:-10}
KEEP_PREV_IMAGES=${KEEP_PREV_IMAGES:-2}   # сколько предыдущих образов держать для отката без реестра

REGISTRY=${IMAGE%%/*}
NETWORK=b24sdbot-internal
# Своё имя в реестре — граница уборки образов. Константа, а не вывод из IMAGE:
# ручная выкатка (docs/80-deploy.md §8.10) несёт IMAGE=b24sdbot:manual-…, и уборка
# по такому имени пошла бы не туда. Машина общая с чужим продом, шире не заходим.
OWN_REPO=ghcr.io/devondevceo/devon_b24_support_bot

cd "$APP_DIR"

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "ОШИБКА: $*"; exit 1; }

# --- одна выкатка за раз ----------------------------------------------------
# 16.08 две сессии выкатывались одновременно и по очереди затирали друг друга.
exec 9>"$APP_DIR/.deploy.lock"
flock -n 9 || fail "выкатка уже идёт (.deploy.lock занят)"

switched=0
verified=0   # релиз проверен снаружи — после этого отказ уже не повод откатывать образ
dump=""
prev_image=""
prev_head=""
new_digest=""

cleanup() {
  local rc=$?
  [ -n "$GHCR_USER" ] && docker logout "$REGISTRY" >/dev/null 2>&1 || true
  if [ "$rc" -ne 0 ] && [ "$verified" -eq 1 ]; then
    log "релиз $IMAGE проверен и работает, упало то, что после проверки, — образ не откатываю"
  elif [ "$rc" -ne 0 ] && [ "$switched" -eq 1 ] && [ -n "$prev_image" ]; then
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

disk_used() { df --output=pcent "$APP_DIR" | tail -1 | tr -dc '0-9'; }

# Свои старые образы. `image prune` в уборке их не видит — он берёт только висячие,
# а у каждой выкатки свой тег sha-<12>, и такие теги копились навсегда: 14.09.2026
# их было 26 при диске на 86%, и выкатку пришлось готовить ручной чисткой.
#
# Оставляем текущий и KEEP_PREV_IMAGES предыдущих — то, на что rollback.sh
# откатывается без реестра. «Предыдущие» — по журналу выкаток, а не по дате
# сборки: дата отвечает на вопрос «когда собран», а откату нужен другой — «что
# здесь работало». После отката старый образ снова текущий; образ неудавшейся
# выкатки самый свежий и не работал ни минуты; коммит, не меняющий образ (журнал,
# документы), собирается в тот же образ с той же датой. Журнал пишет этот скрипт
# и читает rollback.sh — других источников, понятных обоим, нет.
#
# Считаем образы, а не теги: у пары тегов одного образа (так на сервере лежит
# каждый релиз со следующей за ним записью в журнал) одно место под откат,
# и оба тега живут, пока образ нужен.
#
# Удаляем `docker rmi <тег>` без -f: образ, на котором стоит контейнер, docker
# удалить откажется сам. Отказ — строка в логе, а не провал выкатки. Вызывается
# только через `|| true`, поэтому errexit внутри не действует — каждый шаг,
# после сбоя которого удалять нельзя, проверен явно.
prune_own_images() {
  local want listing repo tag id ref err removed=0 refused=0
  local -A id_of=() keep=()
  local -a own=() held=() extra=()
  want=$((10#$KEEP_PREV_IMAGES + 1))

  # Фильтр демона — первый рубеж, точное сравнение имени — второй: шире своего
  # имени уборка не заходит, как бы демон ни понял фильтр.
  listing=$(docker images --no-trunc --format '{{.Repository}} {{.Tag}} {{.ID}}' "$OWN_REPO") \
    || { log "свои образы: docker images не ответил — ничего не удаляю"; return 1; }
  while read -r repo tag id; do
    [ "$repo" = "$OWN_REPO" ] && [ -n "$id" ] || continue
    case $tag in ''|'<none>') continue ;; esac
    id_of[$repo:$tag]=$id
    own+=("$repo:$tag")
  done <<< "$listing"

  # Текущий — по digest запущенных контейнеров, что бы ни было записано в журнале.
  [ -n "$new_digest" ] && keep[$new_digest]=1
  # Журнал от свежих записей к старым: выкаченный образ, затем тот, что работал
  # до него. Образ, которого на диске уже нет, места под откат не занимает.
  while IFS= read -r ref; do
    [ "${#keep[@]}" -lt "$want" ] || break
    [ -n "$ref" ] || continue
    id=${id_of[$ref]:-}
    [ -n "$id" ] && keep[$id]=1
  done < <(tac .deploy/history | cut -f2,4 | tr '\t' '\n')

  for ref in "${own[@]}"; do
    id=${id_of[$ref]}
    if [ -n "${keep[$id]+x}" ]; then held+=("${ref#"$OWN_REPO":}"); else extra+=("$ref"); fi
  done
  log "образы для отката: ${#keep[@]} из $want (${held[*]:-своих тегов нет}), лишних тегов: ${#extra[@]}"
  [ "${#extra[@]}" -gt 0 ] || return 0

  # Журнал короче, чем нужно откату, — значит, неизвестно, нет ли среди лишних
  # тегов недостающих предыдущих (журнал стёрт или сервер новый). Не удаляем
  # ничего: через пару выкаток журнал сам дорастёт до нужной длины.
  if [ "${#keep[@]}" -lt "$want" ]; then
    log "журнал выкаток короче, чем нужно откату, — лишние теги не трогаю"
    return 1
  fi

  for ref in "${extra[@]}"; do
    if err=$(docker rmi "$ref" 2>&1 >/dev/null </dev/null); then
      removed=$((removed + 1))
      log "удалён $ref"
    else
      refused=$((refused + 1))
      log "не удалён $ref: ${err%%$'\n'*}"
    fi
  done
  log "уборка своих тегов: удалено $removed, отказов $refused"
}

trap cleanup EXIT

log "выкатка $IMAGE (коммит $GIT_SHA)"

# Опечатка в числе иначе всплыла бы только в уборке, уже после выкатки.
[[ $KEEP_PREV_IMAGES =~ ^[0-9]+$ ]] \
  || fail "KEEP_PREV_IMAGES='$KEEP_PREV_IMAGES' — нужно целое число, 0 и больше"

# --- 1. место на диске ------------------------------------------------------
used=$(disk_used)
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

# Обещание из шапки — «образ, который уже лежит локально, выкатывается без
# обращения к реестру» — до 09.09.2026 не выполнялось: `docker pull` стоял
# безусловно, и собранный руками образ ронял выкатку на попытке скачать его
# из Docker Hub. Аварийный путь без CI (§8.10 docs/80-deploy.md) обязан работать,
# иначе он существует только на бумаге.
#
# Реестр спрашиваем, когда есть у кого: мы залогинились (значит образ пришёл
# оттуда и тег мог сдвинуться) либо образа нет в местном демоне.
if [ -n "$GHCR_USER" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker pull "$IMAGE" >/dev/null || fail "не скачался образ $IMAGE"
else
  log "образ взят из местного демона, реестр не спрашиваем"
fi
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
# Хвост в 20 строк, а не в 3: pg_dump 16.13 дописывает ПОСЛЕ маркера завершения
# строку `\unrestrict <токен>` (защита от подмены команд при восстановлении,
# добавлена патчами 2025 года) и пустые строки. Проверка по трём последним
# строкам объявляла целый дамп оборванным и роняла выкатку — поймано на первом
# же прогоне с рабочим ключом, 19.08.2026.
{ gzip -dc "$dump" || true; } | tail -20 | grep -q 'PostgreSQL database dump complete' \
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

# Публичный адрес спрашиваем С ПОВТОРОМ. «Контейнер healthy» и «Traefik уже знает
# про новый контейнер» — разные события: маршрут обновляется по событию Docker,
# с задержкой в секунду-другую. Одиночный curl поймал этот зазор 19.08.2026 и
# откатил совершенно рабочий релиз — то есть проверка навредила ровно там, где
# должна была защитить. Ждём до PUBLIC_TIMEOUT, а не спрашиваем один раз.
deadline=$((SECONDS + PUBLIC_TIMEOUT))
until curl -fsS --max-time 15 "$HEALTH_URL" 2>/dev/null | grep -q '"status":"ok"'; do
  [ "$SECONDS" -lt "$deadline" ] || fail "$HEALTH_URL не отвечает ok за ${PUBLIC_TIMEOUT} с"
  sleep 3
done
log "$HEALTH_URL отвечает ok"

# Релиз проверен снаружи. Всё ниже — учёт и уборка, и их сбой не повод откатывать
# работающий образ: откат здесь навредил бы ровно там, где выкатка уже удалась.
verified=1

# --- 9. журнал и уборка -----------------------------------------------------
# Журнал пишется ДО уборки: по нему уборка решает, какие образы нужны откату.
printf '%s\t%s\t%s\t%s\n' "$(date -Is)" "$IMAGE" "$GIT_SHA" "${prev_image:-—}" >> .deploy/history

# image prune БЕЗ -a: с -a он снёс бы неиспользуемые образы соседей по машине.
# Поэтому свои старые теги — отдельным шагом и только под своим именем.
prune_own_images || true
docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
docker image prune -f --filter until=168h >/dev/null 2>&1 || true
ls -1t backups/pre-deploy-*.sql.gz 2>/dev/null | tail -n +$((KEEP_DUMPS + 1)) | xargs -r rm -f

# Следующая выкатка начнётся с проверки диска — пусть запас виден уже сейчас.
log "диск после уборки: занято $(disk_used)%, предел выкатки ${DISK_MAX_PCT}%"
log "готово: $IMAGE"
