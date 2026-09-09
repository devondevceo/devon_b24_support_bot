"""Страж install.sh: серверный скрипт обновления обязан совпадать с CI.

`install.sh update` подтягивает main и выкатывает образ, собранный CI для
этого коммита. Тег образа скрипт вычисляет сам — и если формула разойдётся
с той, по которой CI образ публикует, скрипт молча уйдёт искать
несуществующий тег. Отказ будет выглядеть как «CI ещё не собрал»: та же
строка, тот же вид, но ждать бесполезно.

Второе расхождение того же рода — список файлов, которые выкатка пишет поверх
дерева (`docker-compose.yml`, `scripts/*.sh`). Скрипт возвращает их к HEAD
молча, всё остальное считает чужой правкой и останавливается. Появись в
deploy.yml четвёртый копируемый файл — обновление начало бы отказывать
на ровном месте, и виноватым выглядел бы сервер, а не рассинхрон двух списков.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
# Комментарии в скрипте объясняют, чего делать НЕЛЬЗЯ, и потому содержат ровно
# те строки, которые ищут запреты ниже. Сверяем код, а не рассуждения о нём.
CODE = "\n".join(ln for ln in INSTALL.splitlines() if not ln.lstrip().startswith("#"))
DEPLOY_YML = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")


def test_script_exists_and_is_bash() -> None:
    assert INSTALL.startswith("#!/usr/bin/env bash\n")
    assert "set -Eeuo pipefail" in INSTALL


def test_image_tag_formula_matches_ci() -> None:
    """CI публикует `sha-<12>`; скрипт обязан просить ровно этот тег."""
    ci = re.search(r"sha-\$\{GITHUB_SHA::(\d+)\}", DEPLOY_YML)
    assert ci, "в deploy.yml не нашлась формула тега образа"

    tag = re.search(r"printf 'ghcr\.io/%s:sha-%s\\n'.*\$\{1:0:(\d+)\}", INSTALL)
    assert tag, "в install.sh не нашлась формула тега образа"

    assert tag.group(1) == ci.group(1)


def test_repo_slug_is_lowercased_like_ci() -> None:
    """Реестр не различает регистр на входе, но различает в имени пакета."""
    assert "tr '[:upper:]' '[:lower:]'" in DEPLOY_YML
    assert "tr '[:upper:]' '[:lower:]'" in INSTALL


def test_deploy_owned_files_match_what_ci_copies() -> None:
    """Список «файлов выкатки» ведётся в двух местах — сверяем по исходнику."""
    commands = re.findall(r"scp\b(?:[^\n]*\\\n)*[^\n]*", DEPLOY_YML)
    assert commands, "в deploy.yml не нашлось ни одного scp"
    copied = {Path(t).name
              for cmd in commands
              for t in re.findall(r"[\w./-]+\.(?:yml|sh)", cmd)}

    owned = re.search(r"DEPLOY_OWNED=\(([^)]*)\)", INSTALL)
    assert owned
    names = {Path(p).name for p in owned.group(1).split()}

    assert names == copied


def test_token_file_is_ignored_by_git() -> None:
    """Файл с токеном лежит В КАТАЛОГЕ приложения, а он теперь чекаут main.

    Переименовали файл в скрипте, забыли `.gitignore` — и боевой токен реестра
    ждёт первого же `git add .` на сервере. Инвариант И-7 проверяется в CI
    gitleaks-ом, но там он ловит уже сделанный коммит, а здесь — возможность.
    """
    files = re.search(r"TOKEN_FILES=\(([^)]*)\)", INSTALL)
    assert files, "в install.sh не нашлись пути файла с токеном"
    names = {Path(p.strip('"')).name for p in files.group(1).split()}

    ignored = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for name in names:
        assert name in ignored, f"{name} не закрыт .gitignore"


def test_token_file_permissions_are_checked() -> None:
    """Токен, читаемый всей машиной, — это токен соседей по общему VPS."""
    assert re.search(r"600\|400", INSTALL)


def test_token_is_read_outside_command_substitution() -> None:
    """`fail` внутри `$( )` завершает подоболочку, а не скрипт.

    Поймано живым прогоном: файл с правами 644 напечатал отказ и не остановил
    ничего — работа шла дальше с пустым токеном и падала в конце совсем другой
    ошибкой. Проверка, которая не останавливает, хуже отсутствующей.
    """
    assert "$(read_token)" not in CODE
    assert "read_token\n" in CODE or "read_token " in CODE


def test_server_never_builds_images() -> None:
    """docs/10-architecture.md §10: на сервере не собирается ничего."""
    assert "docker build" not in CODE
    assert "compose build" not in CODE


def test_latest_is_never_a_deploy_target() -> None:
    """`:latest` — образ ПРЕДЫДУЩЕЙ удачной сборки.

    Подставить его вместо несобранного значило бы выкатить назад под видом
    обновления: команда отчиталась бы успехом, а прод уехал бы на коммит назад.
    """
    assert ":latest" not in CODE


def test_update_delegates_to_deploy_script() -> None:
    """Вторая выкатка рядом с первой разойдётся с ней — и в худший момент."""
    assert 'bash "$APP_DIR/scripts/deploy.sh"' in INSTALL
    for own_step in ("pg_dump", "alembic upgrade", "docker compose up"):
        assert own_step not in CODE, f"install.sh дублирует шаг выкатки: {own_step}"
