"""Пульс фоновых процессов.

У бота и воркера нет HTTP-порта, поэтому healthcheck для них — не curl.
Раньше воркер наследовал HEALTHCHECK из образа (`curl localhost:8000/health`)
и вечно числился `unhealthy`, а у бота стояла заглушка `python -c "sys.exit(0)"`,
которая проходила всегда — то есть не проверяла ничего. Оба варианта одинаково
бесполезны: живость процесса подтверждает только сам цикл.

Цикл на каждом успешном обороте зовёт `beat()`, а healthcheck запускает
`python -m b24bot.core.heartbeat <максимальный_возраст_секунд>` и валится,
если файл устарел или его нет. Отдельный файл на процесс, потому что в образе
один и тот же код запускается тремя контейнерами.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

DIR = Path(os.environ.get("HEARTBEAT_DIR") or tempfile.gettempdir()) / "b24bot"


def path_for(name: str) -> Path:
    return DIR / f"{name}.beat"


def beat(name: str) -> None:
    """Отметить оборот цикла. Ошибки записи не должны валить рабочий цикл."""
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = path_for(name).with_suffix(".tmp")
        tmp.write_text(str(time.time()), encoding="ascii")
        tmp.replace(path_for(name))
    except OSError:
        pass


def age(name: str) -> float | None:
    """Сколько секунд назад был последний оборот. None — пульса не было вовсе."""
    try:
        return time.time() - float(path_for(name).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m b24bot.core.heartbeat <name> <max_age_seconds>")
        return 2
    name, limit = argv[1], float(argv[2])
    seen = age(name)
    if seen is None:
        print(f"пульса {name} нет")
        return 1
    if seen > limit:
        print(f"пульс {name} устарел на {seen:.0f} с при пределе {limit:.0f} с")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
