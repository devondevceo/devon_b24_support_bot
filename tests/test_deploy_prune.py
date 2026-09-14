"""Уборка своих образов в `scripts/deploy.sh` — прогоном настоящего скрипта.

Скрипт идёт целиком, от блокировки до «готово»: подменены только внешние команды —
`docker`, `curl`, `df` и `flock` — заглушками в начале PATH. Так проверяется не
функция уборки сама по себе, а то, что она стоит где нужно: после проверенной
выкатки, по журналу, который выкатка только что дописала, и так, что её отказ
не откатывает рабочий релиз.

Подделка `docker` ведёт склад образов в текстовом файле и отвечает в том виде, в
каком отвечал настоящий docker 29 на сервере (id полный только с `--no-trunc`,
`rmi` образа под контейнером — отказ `conflict`). На `docker images` она отдаёт
ВЕСЬ склад, не глядя на фильтр по имени, — самый широкий ответ, какой только может
дать демон. Раз чужие образы уцелели при таком ответе, их бережёт сравнение имени
в самом скрипте, а не удача с фильтром.

Имена соседей — настоящие соседи по машине (docs/10-architecture.md): уборка
обязана пройти мимо них при любом раскладе.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy.sh"
REPO = "ghcr.io/devondevceo/devon_b24_support_bot"


def find_bash() -> str | None:
    found = shutil.which("bash")
    if os.name != "nt":
        return found
    # На Windows годится только Git Bash: bash.exe из System32 — это WSL, и путей
    # Windows, где лежат временные каталоги теста, он не понимает.
    if found and "system32" not in found.lower():
        return found
    git = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    return str(git) if git.exists() else None


BASH = find_bash()

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Подделка docker для tests/test_deploy_prune.py — ровно те вызовы, что делает
# scripts/deploy.sh. Склад: $FAKE_DIR/images, строка «репозиторий тег id».
set -euo pipefail
D=$FAKE_DIR
S=$D/images
printf '%s\n' "$*" >> "$D/calls"

die()     { echo "подделка: $*" >&2; exit 98; }
no_such() { echo "Error response from daemon: No such image: $1" >&2; exit 1; }
# ссылка без тега — это :latest, как у настоящего docker
norm()    { case ${1##*/} in *:*) printf '%s' "$1" ;; *) printf '%s:latest' "$1" ;; esac; }
# «репозиторий:тег» или полный id → id; пусто, если такого нет
id_of()   { awk -v r="$(norm "$1")" '$1 ":" $2 == r || $3 == r { print $3; exit }' "$S"; }
running() { id_of "$(cat "$D/running_ref" 2>/dev/null || true)"; }
# id занят контейнером: работающим сервисом или тем, что тест положил в held
in_use()  { [ "$1" = "$(running)" ] || grep -qxF "$1" "$D/held"; }
fmt_of() {  # значение --format среди аргументов
  local p=""
  for a in "$@"; do
    if [ "$p" = --format ]; then printf '%s' "$a"; return; fi
    p=$a
  done
}

images() {
  local fmt trunc=1 line repo tag id
  if [ "${FAKE_IMAGES_FAIL:-}" = 1 ]; then
    echo "Cannot connect to the Docker daemon" >&2; exit 1
  fi
  for a in "$@"; do if [ "$a" = --no-trunc ]; then trunc=0; fi; done
  fmt=$(fmt_of "$@"); [ -n "$fmt" ] || die "images без --format"
  while read -r repo tag id; do
    if [ "$trunc" = 1 ]; then id=${id#sha256:}; id=${id:0:12}; fi
    line=${fmt//'{{.Repository}}'/"$repo"}
    line=${line//'{{.Tag}}'/"$tag"}
    line=${line//'{{.ID}}'/"$id"}
    case $line in *'{{'*) die "неизвестное поле в --format: $fmt" ;; esac
    printf '%s\n' "$line"
  done < "$S"
}

rmi() {
  local ref id
  for ref in "$@"; do
    case $ref in -f|--force) echo "подделка: rmi $ref запрещён" >&2; exit 97 ;; esac
    case $ref in *:*) ;; *) echo "подделка: rmi не по тегу: $ref" >&2; exit 97 ;; esac
  done
  for ref in "$@"; do
    id=$(id_of "$ref"); [ -n "$id" ] || no_such "$ref"
    if [ "$(awk -v i="$id" '$3 == i' "$S" | wc -l)" -eq 1 ] && in_use "$id"; then
      echo "Error response from daemon: conflict: unable to remove repository" \
           "reference \"$ref\" (must force) - container 5f0c1a2b3c4d is using" \
           "its referenced image ${id:7:12}" >&2
      exit 1
    fi
    awk -v r="$ref" '$1 ":" $2 != r' "$S" > "$S.new" && mv "$S.new" "$S"
    echo "Untagged: $ref"
  done
}

image_inspect() {
  local fmt ref="" skip=0 id
  fmt=$(fmt_of "$@")
  for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case $a in --format) skip=1 ;; -*) ;; *) ref=$a ;; esac
  done
  id=$(id_of "$ref"); [ -n "$id" ] || no_such "$ref"
  case $fmt in
    '')        echo "[{\"Id\": \"$id\"}]" ;;
    '{{.Id}}') echo "$id" ;;
    *)         die "image inspect --format $fmt" ;;
  esac
}

case $1 in
  login)   cat >/dev/null ;;
  logout)  ;;
  pull)
    r=$(norm "$2")
    [ -n "$(id_of "$r")" ] || printf '%s %s %s\n' "${r%:*}" "${r##*:}" "$FAKE_PULL_ID" >> "$S" ;;
  images)  shift; images "$@" ;;
  rmi)     shift; rmi "$@" ;;
  image)
    case $2 in
      inspect) shift 2; image_inspect "$@" ;;
      rm)      shift 2; rmi "$@" ;;
      prune)
        case " $* " in
          *" -a "*|*" --all "*|*" -af "*) echo "подделка: image prune -a запрещён" >&2; exit 97 ;;
        esac ;;
      *)       die "image $2" ;;
    esac ;;
  builder) ;;
  inspect)
    case $(fmt_of "$@") in
      '{{.Config.Image}}') cat "$D/running_ref" ;;
      '{{.Image}}')        running ;;
      *.State.Health*)     echo healthy ;;
      *)                   die "inspect $*" ;;
    esac ;;
  run)
    case " $* " in
      *" alembic current "*)      echo "0020_rls (head)" ;;
      *" alembic upgrade head "*) ;;
      *)                          die "run $*" ;;
    esac ;;
  compose)
    case $2 in
      exec)   # хвост как у pg_dump 16.13: маркер завершения — не последняя строка
              printf -- '%s\n' '-- PostgreSQL database dump' 'SELECT 1;' \
                '-- PostgreSQL database dump complete' '' '\unrestrict fake' '' ;;
      config) ;;
      up)     [ -n "${APP_IMAGE:-}" ] || die "compose up без APP_IMAGE"
              printf '%s\n' "$APP_IMAGE" > "$D/running_ref" ;;
      ps)     echo "cid-${4:-}" ;;
      *)      die "compose $2" ;;
    esac ;;
  *) die "не умею: $*" ;;
esac
"""

FAKE_CURL = """#!/usr/bin/env bash
echo '{"status":"ok","checks":{"db":"ok","miniapp":"ok"}}'
"""
FAKE_DF = """#!/usr/bin/env bash
printf 'Use%%\\n %s%%\\n' 42
"""
FAKE_FLOCK = """#!/usr/bin/env bash
exit 0
"""

# Соседи по машине и обманки: имена, похожие на наше, начинающиеся с него,
# кончающиеся им. Ни одно из них уборка трогать не вправе.
NEIGHBOURS = [
    "ghcr.io/devondevceo/devon-website:latest",
    "mclick_landing-web:latest",
    "devon-billing-app:prod",
    "postgres:16-alpine",
    "traefik:v3.1",
    "b24sdbot-api:latest",
    "b24sdbot:manual-20260909-1200",
    "ghcr.io/devondevceo/devon_b24_support_bot-web:sha-000000000001",
    "ghcr.io/devondevceo/devon_b24_support_bo:sha-000000000002",
    "mirror.example/ghcr.io/devondevceo/devon_b24_support_bot:sha-000000000003",
]


def img_id(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def ref(tag: str) -> str:
    return f"{REPO}:{tag}"


def posix(p: Path) -> str:
    return p.as_posix()


def journal(*deploys: str) -> list[str]:
    """Строки `.deploy/history` для выкаток по порядку: у каждой — предыдущий образ."""
    lines, prev = [], "—"
    for n, image in enumerate(deploys):
        lines.append(f"2026-09-{n + 1:02d}T12:00:00+03:00\t{image}\tsha{n:02d}\t{prev}")
        prev = image
    return lines


@dataclass
class Run:
    code: int
    out: str
    calls: list[str]
    store: dict[str, str]   # «репозиторий:тег» → id: что осталось на складе
    env_file: str
    history: list[str]

    @property
    def removed(self) -> list[str]:
        return [c.split(" ", 1)[1] for c in self.calls if c.startswith("rmi ")]

    @property
    def kept_own(self) -> set[str]:
        return {r for r in self.store if r.startswith(REPO + ":")}


def deploy(
    tmp_path: Path,
    *,
    store: dict[str, str],
    history: list[str] | None,
    running: str,
    image: str,
    pull_id: str,
    keep_prev: str | None = None,
    held: tuple[str, ...] = (),
    images_fail: bool = False,
    history_is_dir: bool = False,
) -> Run:
    if BASH is None:
        pytest.skip("нужен bash: deploy.sh прогоняется настоящим")
    app, fake, bin_dir = tmp_path / "app", tmp_path / "fake", tmp_path / "bin"
    for d in (app / ".deploy", fake, bin_dir):
        d.mkdir(parents=True)
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL),
                       ("df", FAKE_DF), ("flock", FAKE_FLOCK)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    lines = []
    for full, ident in store.items():
        repo, tag = full.rsplit(":", 1) if ":" in full.rsplit("/", 1)[-1] else (full, "<none>")
        lines.append(f"{repo} {tag} {ident}")
    (fake / "images").write_text("".join(f"{x}\n" for x in lines), encoding="utf-8", newline="\n")
    (fake / "running_ref").write_text(running + "\n", encoding="utf-8", newline="\n")
    (fake / "held").write_text("".join(f"{x}\n" for x in held), encoding="utf-8", newline="\n")
    (fake / "calls").write_text("", encoding="utf-8")

    (app / ".env").write_text(
        "DATABASE_URL=postgresql://test:test@postgres:5432/test\n"
        f"APP_IMAGE={running}\n"
        "PUBLIC_BASE_URL=https://b24sdbot.example\n",
        encoding="utf-8", newline="\n",
    )
    (app / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8", newline="\n")
    hist = app / ".deploy" / "history"
    if history_is_dir:
        hist.mkdir()
    elif history is not None:
        hist.write_text("".join(f"{x}\n" for x in history), encoding="utf-8", newline="\n")

    env = {
        **os.environ,
        "FAKE_BIN": posix(bin_dir),
        "DEPLOY_SH": posix(DEPLOY),
        "APP_DIR": posix(app),
        "IMAGE": image,
        "GHCR_USER": "ci-bot",
        "GIT_SHA": "0123456789abcdef0123456789abcdef01234567",
        "FAKE_DIR": posix(fake),
        "FAKE_PULL_ID": pull_id,
        "FAKE_IMAGES_FAIL": "1" if images_fail else "",
    }
    env.pop("KEEP_PREV_IMAGES", None)
    if keep_prev is not None:
        env["KEEP_PREV_IMAGES"] = keep_prev

    # Заглушки ставятся в начало PATH уже внутри bash: Git Bash на Windows дописывает
    # свои каталоги вперёд, и настоящий curl из /usr/bin иначе обогнал бы подделку.
    # `cd && pwd` приводит путь к виду, который понимает сам bash (/c/… в Git Bash).
    launch = 'export PATH="$(cd "$FAKE_BIN" && pwd):$PATH" && exec "$BASH" "$DEPLOY_SH"'
    done = subprocess.run(  # noqa: S603 — свой скрипт и свои заглушки, чужого ввода нет
        [BASH, "-c", launch], input="токен-реестра\n", env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    left = {}
    for row in (fake / "images").read_text(encoding="utf-8").splitlines():
        repo, tag, ident = row.split(" ")
        left[f"{repo}:{tag}"] = ident
    return Run(
        code=done.returncode,
        out=done.stdout + done.stderr,
        calls=(fake / "calls").read_text(encoding="utf-8").splitlines(),
        store=left,
        env_file=(app / ".env").read_text(encoding="utf-8"),
        history=hist.read_text(encoding="utf-8").splitlines() if hist.is_file() else [],
    )


def releases(n: int) -> list[str]:
    """Теги n выкаток по порядку, от старой к свежей; у каждой свой образ."""
    return [f"sha-{i:012d}" for i in range(1, n + 1)]


NEW = "sha-ffffffffffff"

# Сервер на вечер 14.09.2026, снято чтением: журнал помнит все выкатки, на диске три
# образа, и у двух — по паре тегов. За каждой выкаткой шёл коммит «Журнал: …», а он не
# трогает ничего из того, что входит в образ, — образ из него тот же и с той же датой.
SERVER_JOURNAL = ["sha-0f4f887945b1", "sha-f852156b1aad", "sha-94a0f0484687",
                  "sha-3145af76783c", "sha-887b547c365f", "sha-23c115cc2035", "sha-3c0dafa8542a"]
SERVER_DISK = {"sha-94a0f0484687": "C", "sha-3145af76783c": "B", "sha-887b547c365f": "B",
               "sha-23c115cc2035": "A", "sha-3c0dafa8542a": "A"}


def server_store() -> dict[str, str]:
    return {ref(t): img_id(i) for t, i in SERVER_DISK.items()} | {n: img_id(n) for n in NEIGHBOURS}


def base_store(tags: list[str]) -> dict[str, str]:
    store = {ref(t): img_id(t) for t in tags}
    store.update({n: img_id(n) for n in NEIGHBOURS})
    store[f"{REPO}:<none>"] = img_id("digest-only")   # остался от pull по digest
    store["<none>:<none>"] = img_id("dangling")
    return store


# --- главное: удаляются только старые теги своего репозитория -------------------------

def test_only_old_tags_of_own_repo_go(tmp_path: Path) -> None:
    tags = releases(8)
    store = base_store(tags)
    store[ref("sha-f00f00f00f00")] = img_id("неудавшаяся выкатка")   # в журнале её нет
    store[ref("latest")] = img_id(tags[2])                              # старый latest
    before = dict(store)

    run = deploy(tmp_path, store=store, history=journal(*map(ref, tags)),
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW))

    assert run.code == 0, run.out
    # Текущий и два предыдущих по журналу; остальное своё — прочь, включая
    # неудавшуюся выкатку и старый latest, которых откат не знает.
    assert run.kept_own == {ref(NEW), ref(tags[-1]), ref(tags[-2]), f"{REPO}:<none>"}
    assert sorted(run.removed) == sorted(
        [ref(t) for t in tags[:-2]] + [ref("sha-f00f00f00f00"), ref("latest")]
    )
    # Соседи, обманки и висячие — ровно как были.
    for name in [*NEIGHBOURS, "<none>:<none>"]:
        assert run.store.get(name) == before[name], name
    # Фильтр демону передан — первый рубеж; второй проверен тем, что подделка его
    # проигнорировала и отдала весь склад, а соседи целы.
    assert "images --no-trunc --format {{.Repository}} {{.Tag}} {{.ID}} " + REPO in run.calls
    assert f"удалён {ref(tags[0])}" in run.out
    assert "уборка своих тегов: удалено 8, отказов 0" in run.out
    # Выкатка записана, образ не откатывался.
    assert f"APP_IMAGE={ref(NEW)}" in run.env_file
    assert run.history[-1].split("\t")[1:] == [
        ref(NEW), "0123456789abcdef0123456789abcdef01234567", ref(tags[-1])]


def test_no_force_and_no_prune_all(tmp_path: Path) -> None:
    tags = releases(5)
    run = deploy(tmp_path, store=base_store(tags), history=journal(*map(ref, tags)),
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW))

    assert run.code == 0, run.out
    for call in run.calls:
        words = call.split()
        if words[:1] == ["rmi"] or words[:2] == ["image", "rm"]:
            assert not {"-f", "--force"} & set(words), call
        if "prune" in words[:2]:
            assert not {"-a", "--all", "-af", "-fa"} & set(words), call


# --- что считается «предыдущим» -------------------------------------------------------

def test_one_image_under_two_tags_takes_one_slot(tmp_path: Path) -> None:
    """Первый релиз, меняющий образ, на сегодняшнем сервере. Текущий новый и два
    предыдущих ОБРАЗА, A и B, — каждый со своей парой тегов. Мест под откат три,
    и третье не ушло на второй тег того же образа: удаляется C."""
    run = deploy(tmp_path, store=server_store(), history=journal(*map(ref, SERVER_JOURNAL)),
                 running=ref(SERVER_JOURNAL[-1]), image=ref(NEW), pull_id=img_id("E"))

    assert run.code == 0, run.out
    assert run.kept_own == {ref(NEW), *(ref(t) for t, i in SERVER_DISK.items() if i != "C")}
    assert run.removed == [ref("sha-94a0f0484687")]


def test_release_without_image_change_removes_nothing(tmp_path: Path) -> None:
    """Первая выкатка после этой правки: скрипты и документы в образ не входят,
    образ тот же, и он лишь получает третий тег. Удалять нечего — на диске ровно
    текущий и два предыдущих."""
    run = deploy(tmp_path, store=server_store(), history=journal(*map(ref, SERVER_JOURNAL)),
                 running=ref(SERVER_JOURNAL[-1]), image=ref(NEW), pull_id=img_id("A"))

    assert run.code == 0, run.out
    assert run.removed == []
    assert "образы для отката: 3 из 3" in run.out
    assert "лишних тегов: 0" in run.out


def test_order_comes_from_journal_not_from_build_date(tmp_path: Path) -> None:
    """Дата сборки врёт об откате трижды. X собран раньше всех, но после отката
    на него работает последним; Y собран позже X и откачен как неудачный; Z собран
    позже всех и не проработал ни минуты — выкатка упала. Откату нужен X."""
    x, y, z = "sha-00000000000a", "sha-00000000000b", "sha-00000000000c"
    history = [*journal(ref(x), ref(y)),
               f"2026-09-05T12:00:00+03:00\t{ref(x)}\tоткат\t—"]   # строка rollback.sh
    store = {ref(x): img_id(x), ref(y): img_id(y), ref(z): img_id(z)}

    run = deploy(tmp_path, store=store, history=history, running=ref(x),
                 image=ref(NEW), pull_id=img_id(NEW), keep_prev="1")

    assert run.code == 0, run.out
    assert run.kept_own == {ref(NEW), ref(x)}
    assert sorted(run.removed) == sorted([ref(y), ref(z)])


def test_image_gone_from_disk_takes_no_slot(tmp_path: Path) -> None:
    """Так сервер выглядит после ручной чистки 14.09: журнал помнит все выкатки,
    а на диске осталась часть. Образ, которого нет, откату не поможет — место
    под откат достаётся следующему по журналу из тех, что на диске есть."""
    tags = releases(6)
    store = base_store(tags)
    del store[ref(tags[4])]   # предпоследний удалили руками

    run = deploy(tmp_path, store=store, history=journal(*map(ref, tags)),
                 running=ref(tags[5]), image=ref(NEW), pull_id=img_id(NEW))

    assert run.code == 0, run.out
    assert run.kept_own == {ref(NEW), ref(tags[5]), ref(tags[3]), f"{REPO}:<none>"}
    assert sorted(run.removed) == sorted(ref(t) for t in tags[:3])


def test_running_image_is_kept_however_the_journal_spells_it(tmp_path: Path) -> None:
    """Текущий образ защищает digest запущенных контейнеров, а не запись в журнале.
    Выкатку здесь запустили руками с IMAGE без тега: docker понял её как :latest,
    журнал записал имя как есть — и в листинге такой строки нет."""
    tags = releases(5)
    store = base_store(tags)
    store[ref("sha-eeeeeeeeeeee")] = img_id("E")   # тот же образ под своим sha-тегом

    run = deploy(tmp_path, store=store, history=journal(*map(ref, tags)),
                 running=ref(tags[-1]), image=REPO, pull_id=img_id("E"), keep_prev="1")

    assert run.code == 0, run.out
    assert run.history[-1].split("\t")[1] == REPO
    assert {ref("latest"), ref("sha-eeeeeeeeeeee")} <= run.kept_own
    assert sorted(run.removed) == sorted(ref(t) for t in tags[:-1])


@pytest.mark.parametrize("keep_prev", [0, 1, 3, 7])
def test_keep_prev_images_sets_depth(tmp_path: Path, keep_prev: int) -> None:
    tags = releases(8)
    run = deploy(tmp_path, store=base_store(tags), history=journal(*map(ref, tags)),
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW),
                 keep_prev=str(keep_prev))

    assert run.code == 0, run.out
    kept = [ref(t) for t in tags[len(tags) - keep_prev:]] if keep_prev else []
    assert run.kept_own == {ref(NEW), *kept, f"{REPO}:<none>"}
    assert len(run.removed) == len(tags) - keep_prev


def test_bad_keep_prev_images_stops_before_any_change(tmp_path: Path) -> None:
    tags = releases(3)
    run = deploy(tmp_path, store=base_store(tags), history=journal(*map(ref, tags)),
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW), keep_prev="два")

    assert run.code != 0
    assert "KEEP_PREV_IMAGES='два'" in run.out
    # До уборки не дошло ничего: ни скачивания, ни дампа, ни миграций, ни подъёма.
    assert [c for c in run.calls if c.split()[0] not in {"logout"}] == []
    assert f"APP_IMAGE={ref(tags[-1])}" in run.env_file


# --- отказы уборки не роняют выкатку и не откатывают релиз -----------------------------

def test_refusal_is_logged_not_fatal(tmp_path: Path) -> None:
    """Старый образ держит остановленный контейнер — docker откажет в rmi без -f.
    Выкатка при этом успешна, образ не откатывается, остальное убрано."""
    tags = releases(6)
    run = deploy(tmp_path, store=base_store(tags), history=journal(*map(ref, tags)),
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW),
                 held=(img_id(tags[1]),))

    assert run.code == 0, run.out
    assert f"не удалён {ref(tags[1])}: Error response from daemon: conflict" in run.out
    assert "уборка своих тегов: удалено 3, отказов 1" in run.out
    assert ref(tags[1]) in run.store
    assert f"APP_IMAGE={ref(NEW)}" in run.env_file
    assert sum(c.startswith("compose up") for c in run.calls) == 1   # отката не было
    assert f"готово: {ref(NEW)}" in run.out


def test_listing_failure_removes_nothing(tmp_path: Path) -> None:
    tags = releases(6)
    run = deploy(tmp_path, store=base_store(tags), history=journal(*map(ref, tags)),
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW), images_fail=True)

    assert run.code == 0, run.out
    assert run.removed == []
    assert "docker images не ответил — ничего не удаляю" in run.out
    assert f"APP_IMAGE={ref(NEW)}" in run.env_file


def test_short_journal_removes_nothing(tmp_path: Path) -> None:
    """Журнал стёрт: выкатка знает только себя и того, кого сменила. Остальные
    теги могут быть недостающими предыдущими — их не трогаем, пока журнал не
    дорастёт."""
    tags = releases(6)
    run = deploy(tmp_path, store=base_store(tags), history=None,
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW))

    assert run.code == 0, run.out
    assert run.removed == []
    assert "образы для отката: 2 из 3" in run.out
    assert "журнал выкаток короче, чем нужно откату, — лишние теги не трогаю" in run.out
    assert len(run.history) == 1


def test_failure_after_verification_does_not_roll_back(tmp_path: Path) -> None:
    """Любой сбой после проверки снаружи — повод для красного прогона, но не для
    отката: релиз уже работает. Здесь не пишется журнал (на его месте каталог),
    и уборка, которая по нему решает, до удаления не доходит."""
    tags = releases(4)
    run = deploy(tmp_path, store=base_store(tags), history=None, history_is_dir=True,
                 running=ref(tags[-1]), image=ref(NEW), pull_id=img_id(NEW))

    assert run.code != 0
    assert "образ не откатываю" in run.out
    assert "ОТКАТ" not in run.out
    assert f"APP_IMAGE={ref(NEW)}" in run.env_file
    assert sum(c.startswith("compose up") for c in run.calls) == 1
    assert run.removed == []


# --- стражи по исходнику -----------------------------------------------------------------

def test_own_repo_matches_compose_default() -> None:
    """Граница уборки и имя образа в compose — одно имя. Переименуют репозиторий
    и поправят только compose — уборка молча перестала бы находить свои теги."""
    own = re.search(r"^OWN_REPO=(\S+)$", DEPLOY.read_text(encoding="utf-8"), re.M)
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    defaults = set(re.findall(r"\$\{APP_IMAGE:-([^}]+):latest\}", compose))
    assert own is not None
    assert defaults == {own.group(1)} == {REPO}


@pytest.mark.parametrize("script", ["deploy.sh", "rollback.sh"])
def test_scripts_never_widen_cleanup(script: str) -> None:
    """Машина общая с чужим продом: ни `image prune -a`, ни `rmi -f`, ни уборки
    томов (docs/10-architecture.md, «трогать нельзя»)."""
    code = [line.split("#", 1)[0] for line in
            (ROOT / "scripts" / script).read_text(encoding="utf-8").splitlines()]
    for line in code:
        words = set(line.split())
        if "docker" not in words:
            continue
        assert not {"system", "volume"} & words or "prune" not in words, line
        if "image" in words and "prune" in words:
            assert not {"-a", "--all", "-af", "-fa"} & words, line
        if "rmi" in words or ("image" in words and "rm" in words):
            assert not {"-f", "--force"} & words, line
