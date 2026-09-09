#!/usr/bin/env bash
# Обслуживание боевого каталога бота на сервере (/opt/b24sdbot).
#
#   ./install.sh update     подтянуть main и выкатить образ, собранный CI для этого коммита
#   ./install.sh status     что сейчас в дереве, в реестре и в контейнерах
#   ./install.sh install    сделать каталог git-чекаутом main (идемпотентно, .env не трогает)
#
# «Обновить код» и «обновить бота» — разные вещи, и путать их дорого. `git pull`
# меняет только файлы в каталоге: контейнеры работают из образа, собранного CI,
# бинд-монтов исходников в docker-compose.yml нет (единственный том — pgdata).
# Поэтому `update` делает оба шага подряд: ff-мердж main, затем выкатка образа
# С ТЕМ ЖЕ sha, что у подтянутого коммита. Дерево и прод не расходятся.
#
# На сервере не собирается ничего (docs/10-architecture.md §10). Если CI ещё не
# опубликовал образ для коммита — скрипт ждёт его и, не дождавшись, отказывает
# ДО первой мутации: без дампа, без миграций, без перезапуска контейнеров.
#
# Сама выкатка — scripts/deploy.sh, ровно та же, что запускает CI: дамп базы,
# миграции одноразовым контейнером нового образа, подъём, сверка digest
# запущенных контейнеров с выкаченным образом, ожидание healthy и /health,
# автооткат образа при провале. Дублировать её здесь значило бы завести вторую
# выкатку, которая разойдётся с первой ровно тогда, когда это дороже всего.
#
# Токен реестра нужен для `docker pull` приватного пакета GHCR (PAT со scope
# read:packages): GHCR_TOKEN=… в окружении, --token-file <путь> или ввод
# с терминала. На диск скрипт токен не кладёт.

set -Eeuo pipefail

# --- перезапуск с копии ------------------------------------------------------
# bash читает скрипт по мере выполнения. `git merge` ниже способен переписать
# ЭТОТ файл на середине — и дальше выполнится смесь старого текста с новым.
# Поэтому работаем с копией в /tmp, а оригинал в каталоге пусть меняется.
if [ "${B24_INSTALL_REEXEC:-0}" != 1 ]; then
  b24_self=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  b24_copy=$(mktemp -t b24-install.XXXXXX)
  cat -- "${BASH_SOURCE[0]}" >"$b24_copy"
  export B24_INSTALL_REEXEC=1 APP_DIR="${APP_DIR:-$b24_self}"
  exec bash "$b24_copy" "$@"
fi

CLEANUP_PATHS=("$0")
cleanup() { rm -rf -- "${CLEANUP_PATHS[@]}"; }
trap cleanup EXIT

APP_DIR=${APP_DIR:?не определён каталог приложения}
BRANCH=${BRANCH:-main}
WAIT_IMAGE=${WAIT_IMAGE:-600}     # сколько ждать образ от CI, секунд
POLL=20
DRY_RUN=0
FORCE=0
CODE_ONLY=0
TOKEN_FILE=${TOKEN_FILE:-}
GHCR_TOKEN=${GHCR_TOKEN:-}

# Эти файлы поверх дерева пишет scp из .github/workflows/deploy.yml. Их
# расхождение с HEAD — не чья-то правка, а след выкатки, и чинится молча.
DEPLOY_OWNED=(docker-compose.yml scripts/deploy.sh scripts/rollback.sh)

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '%s  ОШИБКА: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }
git_() { git -C "$APP_DIR" "$@"; }

usage() {
  cat <<'TXT'
Использование: ./install.sh <команда> [ключи]

Команды:
  update     подтянуть origin/main и выкатить образ этого коммита
  status     показать дерево, образы и контейнеры, ничего не меняя
  install    сделать каталог git-чекаутом main (для сервера, где репозитория ещё нет)
  help       эта справка

Ключи update:
  --dry-run          показать, что будет сделано, и выйти (ничего не меняется)
  --code-only        только подтянуть код, выкатку не запускать
  --force            выкатить, даже если этот образ уже запущен
  --wait <сек>       сколько ждать образ от CI (по умолчанию 600, 0 — не ждать)
  --token-file <п>   файл с токеном GHCR (иначе GHCR_TOKEN или ввод с терминала)

Переменные окружения: APP_DIR, BRANCH, WAIT_IMAGE, GHCR_TOKEN, TOKEN_FILE.

Примеры:
  ./install.sh status
  ./install.sh update --dry-run
  GHCR_TOKEN=ghp_… ./install.sh update
TXT
}

# --- разбор аргументов -------------------------------------------------------
cmd=${1:-help}
[ $# -gt 0 ] && shift || true
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run)    DRY_RUN=1 ;;
    --code-only)  CODE_ONLY=1 ;;
    --force)      FORCE=1 ;;
    --wait)       WAIT_IMAGE=${2:?--wait ждёт число секунд}; shift ;;
    --token-file) TOKEN_FILE=${2:?--token-file ждёт путь}; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            fail "неизвестный ключ: $1 (./install.sh help)" ;;
  esac
  shift
done

need() { command -v "$1" >/dev/null 2>&1 || fail "нет команды $1"; }

preflight() {
  need git
  need docker
  [ -d "$APP_DIR" ] || fail "нет каталога $APP_DIR"
  docker info >/dev/null 2>&1 || fail "docker недоступен: запустите от root или из группы docker"
}

# owner/repo из origin, в нижнем регистре — ровно так же, как считает его
# .github/workflows/deploy.yml: `github.repository | tr '[:upper:]' '[:lower:]'`.
repo_slug() {
  local url
  url=$(git_ remote get-url origin) || fail "у каталога нет remote origin"
  printf '%s' "$url" \
    | sed -E 's#^(git@|ssh://git@|https://|http://)##; s#^[^/:]+[:/]##; s#\.git$##' \
    | tr '[:upper:]' '[:lower:]'
}

# Тег образа обязан совпадать с тем, что публикует CI: `sha-${GITHUB_SHA::12}`.
# Разъедься эти две формулы — скрипт молча искал бы несуществующий тег.
image_for() { printf 'ghcr.io/%s:sha-%s\n' "$(repo_slug)" "${1:0:12}"; }

running_image() {
  docker inspect b24sdbot-api --format '{{.Config.Image}}' 2>/dev/null || true
}

# --- код ---------------------------------------------------------------------
sync_code() {
  git_ rev-parse --git-dir >/dev/null 2>&1 \
    || fail "$APP_DIR — не git-репозиторий; сделайте ./install.sh install"

  local branch
  branch=$(git_ symbolic-ref --quiet --short HEAD || true)
  [ "$branch" = "$BRANCH" ] || fail \
    "в каталоге ветка «${branch:-отсоединённый HEAD}», а не $BRANCH; переключитесь руками — молча менять ветку на бою нельзя"

  log "fetch origin/$BRANCH"
  git_ fetch --quiet origin "$BRANCH" || fail "не удалось забрать origin/$BRANCH"

  # Своя правка в дереве и след выкатки выглядят одинаково — «изменённый файл».
  # Второе возвращаем к HEAD молча, первое останавливает работу: затирать чужое
  # без спроса — это ровно тот способ потерять данные, ради которого всё и делалось.
  local -a dirty=() mine=()
  mapfile -t dirty < <(git_ status --porcelain=v1 --untracked-files=no | cut -c4-)
  if [ ${#dirty[@]} -gt 0 ]; then
    local f o owned
    for f in "${dirty[@]}"; do
      owned=0
      for o in "${DEPLOY_OWNED[@]}"; do [ "$f" = "$o" ] && owned=1; done
      [ "$owned" = 1 ] || mine+=("$f")
    done
    [ ${#mine[@]} -eq 0 ] \
      || fail "в дереве свои правки: ${mine[*]} — посмотрите git diff и решите сами"
    log "возвращаю к HEAD файлы, переписанные выкаткой: ${dirty[*]}"
    [ "$DRY_RUN" = 1 ] || git_ checkout -- "${dirty[@]}"
  fi

  if [ "$DRY_RUN" = 1 ]; then
    log "(сухой прогон) ff-мердж не выполняется"
  else
    git_ merge --ff-only "origin/$BRANCH" >/dev/null \
      || fail "ff-мердж не прошёл: дерево разошлось с origin/$BRANCH"
    log "код на $(git_ rev-parse --short HEAD) — $(git_ log -1 --format=%s)"
  fi
}

# --- реестр ------------------------------------------------------------------
read_token() {
  local t=""
  if [ -n "$GHCR_TOKEN" ]; then
    t=$GHCR_TOKEN
  elif [ -n "$TOKEN_FILE" ]; then
    [ -r "$TOKEN_FILE" ] || fail "не читается $TOKEN_FILE"
    IFS= read -r t <"$TOKEN_FILE" || true
  elif [ -t 0 ]; then
    printf 'Токен GHCR (PAT со scope read:packages), Enter — пропустить: ' >&2
    IFS= read -rs t || true
    printf '\n' >&2
  fi
  printf '%s' "$t"
}

# Ждём образ, собранный CI. Пока его нет, не тронуто ничего: ни база, ни
# контейнеры. Молча взять :latest нельзя — это образ ПРЕДЫДУЩЕЙ удачной сборки,
# и «обновление» уехало бы назад, выглядя при этом совершенно успешным.
ensure_image() {
  local image=$1 token=$2 user=$3
  local -a dcfg=()
  if [ -n "$token" ]; then
    local cfg
    cfg=$(mktemp -d)
    CLEANUP_PATHS+=("$cfg")
    # Логин в отдельный каталог конфигурации: /root/.docker не трогаем, машина
    # общая, и чужие креды соседей по VPS не наше дело.
    printf '%s' "$token" | docker --config "$cfg" login ghcr.io -u "$user" --password-stdin >/dev/null \
      || fail "реестр не принял токен (нужен scope read:packages)"
    dcfg=(--config "$cfg")
  fi

  local deadline=$((SECONDS + WAIT_IMAGE)) out
  while :; do
    if out=$(docker "${dcfg[@]}" pull "$image" 2>&1); then
      log "образ есть в реестре: $image"
      return 0
    fi
    case $out in
      *denied*|*unauthorized*|*"authentication required"*)
        # Приватный пакет отвечает `denied` и на несуществующий тег тоже: без
        # токена «нет доступа» и «CI ещё не собрал» неразличимы, и ждать вслепую
        # бессмысленно. С токеном отсутствующий тег даёт другой ответ, и цикл ждёт.
        [ -n "$token" ] \
          && fail "реестр не пустил за $image, хотя токен принят: у него нет доступа к пакету" \
          || fail "реестр не пустил за $image. Токен не задан, а приватный пакет отвечает так же и на отсутствующий тег — задайте GHCR_TOKEN=… (PAT со scope read:packages), чтобы отличить «нет доступа» от «CI ещё не собрал»" ;;
    esac
    if [ "$SECONDS" -ge "$deadline" ]; then
      fail "образа $image в реестре нет. CI собирает его при пуше в $BRANCH: проверьте прогон (gh run list --workflow=deploy.yml) или запустите его (gh workflow run deploy.yml --ref $BRANCH). На сервере ничего не изменено."
    fi
    log "образ ещё не опубликован, жду ${POLL} с (в запасе $((deadline - SECONDS)) с)"
    sleep "$POLL"
  done
}

# --- команды -----------------------------------------------------------------
cmd_update() {
  preflight
  [ -f "$APP_DIR/.env" ] || fail "нет $APP_DIR/.env — это не развёрнутый сервер"
  [ -f "$APP_DIR/scripts/deploy.sh" ] || fail "нет $APP_DIR/scripts/deploy.sh"

  sync_code

  local sha image user
  sha=$(git_ rev-parse "origin/$BRANCH")
  image=$(image_for "$sha")
  user=$(repo_slug); user=${user%%/*}

  if [ "$CODE_ONLY" = 1 ]; then
    log "--code-only: выкатку не запускаю; целевой образ был бы $image"
    return 0
  fi

  local now
  now=$(running_image)
  log "сейчас запущено: ${now:-нет контейнера}"
  log "цель:            $image"

  if [ "$now" = "$image" ] && [ "$FORCE" != 1 ]; then
    log "прод уже на этом коммите — выкатывать нечего (--force, чтобы всё равно)"
    return 0
  fi

  local token
  token=$(read_token)

  if [ "$DRY_RUN" = 1 ]; then
    WAIT_IMAGE=0            # сухой прогон не ждёт CI: он отвечает на вопрос «что сейчас»
    ensure_image "$image" "$token" "$user"
    log "(сухой прогон) дальше пошла бы выкатка scripts/deploy.sh — дамп, миграции, подъём, проверки"
    return 0
  fi

  ensure_image "$image" "$token" "$user"

  log "———"
  log "передаю управление scripts/deploy.sh"
  if [ -n "$token" ]; then
    printf '%s\n' "$token" | APP_DIR="$APP_DIR" IMAGE="$image" GHCR_USER="$user" \
      GIT_SHA="$sha" bash "$APP_DIR/scripts/deploy.sh"
  else
    APP_DIR="$APP_DIR" IMAGE="$image" GIT_SHA="$sha" \
      bash "$APP_DIR/scripts/deploy.sh" </dev/null
  fi
}

cmd_status() {
  preflight
  if git_ rev-parse --git-dir >/dev/null 2>&1; then
    git_ fetch --quiet origin "$BRANCH" 2>/dev/null \
      || log "origin недоступен, показываю локальное состояние"
    local target
    target=$(git_ rev-parse "origin/$BRANCH" 2>/dev/null || git_ rev-parse HEAD)
    printf 'ветка:          %s\n' "$(git_ symbolic-ref --quiet --short HEAD || echo 'отсоединённый HEAD')"
    printf 'дерево:         %s  %s\n' "$(git_ rev-parse --short HEAD)" "$(git_ log -1 --format=%s)"
    printf 'origin/%s:    %s  (отстаём на %s коммит(ов))\n' "$BRANCH" \
      "$(git_ rev-parse --short "$target")" \
      "$(git_ rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo '?')"
    printf 'свои правки:    %s файл(ов)\n' "$(git_ status --porcelain=v1 --untracked-files=no | wc -l)"
    printf 'целевой образ:  %s\n' "$(image_for "$target")"
  else
    printf 'дерево:         не git-репозиторий (./install.sh install)\n'
  fi
  printf '.env APP_IMAGE: %s\n' \
    "$(grep -m1 '^APP_IMAGE=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- || echo '—')"
  printf 'запущено:       %s\n' "$(running_image)"
  echo
  docker compose --project-directory "$APP_DIR" ps \
    --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null || true
  if [ -s "$APP_DIR/.deploy/history" ]; then
    echo
    echo "последние выкатки:"
    tail -3 "$APP_DIR/.deploy/history" | awk -F'\t' '{printf "  %s  %s\n", $1, $2}'
  fi
}

# Первичная привязка каталога к репозиторию — то, чего на сервере может не быть,
# если он разворачивался tar-ом. .env не создаётся и не трогается никогда: в нём
# боевые секреты, и выдумать их скрипт не может.
cmd_install() {
  preflight
  [ -f "$APP_DIR/.env" ] || fail "нет $APP_DIR/.env — сначала разверните сервер по docs/80-deploy.md"

  local url=${ORIGIN_URL:-git@github.com:devondevceo/devon_b24_support_bot.git}

  if git_ rev-parse --git-dir >/dev/null 2>&1; then
    log "репозиторий уже есть: $(git_ remote get-url origin 2>/dev/null || echo 'без origin')"
  else
    local snap
    snap="/root/b24sdbot-pre-install-$(date +%Y%m%d-%H%M%S).tgz"
    log "снимок каталога до правок: $snap"
    tar -czf "$snap" -C "$(dirname "$APP_DIR")" "$(basename "$APP_DIR")"
    git_ init -b "$BRANCH" -q
    git_ remote add origin "$url"
    log "fetch origin/$BRANCH"
    git_ fetch -q origin "$BRANCH"
    git_ reset -q "origin/$BRANCH"        # индекс = main, рабочее дерево не тронуто
    git_ branch --set-upstream-to="origin/$BRANCH" "$BRANCH" >/dev/null
    log "дерево расходится с $BRANCH на $(git_ status --porcelain=v1 -uno | wc -l) файл(ов) — привожу к $BRANCH"
    git_ checkout -- .
  fi

  # Артефакты выкатки живут только на сервере. Прячем их локально: .gitignore
  # репозитория для этого трогать нельзя, он общий для всех.
  local ex="$APP_DIR/.git/info/exclude"
  grep -qx '\.deploy/' "$ex" 2>/dev/null \
    || printf '# Серверные артефакты выкатки.\n.deploy/\n.deploy.lock\n' >>"$ex"
  git_ config pull.ff only    # сервер — зеркало на чтение, merge-коммитам тут неоткуда взяться
  git_ config pull.rebase false
  log "готово: $(git_ log -1 --format='%h %s')"
  log "дальше: ./install.sh status и ./install.sh update"
}

case $cmd in
  update)            cmd_update ;;
  status)            cmd_status ;;
  install|bootstrap) cmd_install ;;
  help|-h|--help)    usage ;;
  *)                 usage; fail "неизвестная команда: $cmd" ;;
esac
