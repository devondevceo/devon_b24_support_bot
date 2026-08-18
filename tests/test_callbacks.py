"""Реестр пространств имён callback-кнопок.

Смысл тот же, что у `test_commands.py`: кнопка, которую человек видит, обязана
что-то делать. Только у команд обещание нарушается заметно («бот не отвечает на
/foo»), а у кнопок — правдоподобно: префикс `p` означал сразу и вариант ответа
опросника, и выбор проекта при создании задачи, роутер разбирал первое, вторая
ветка стояла ниже и была недостижима. Человек в чате с двумя проектами жал
«создать» и получал «диалог устарел». Ни ошибки, ни отличия от честно истёкшего
токена.

Поэтому здесь три проверки одного инварианта: каждый префикс, который бот шлёт,
объявлен в реестре, разбирается ровно одной веткой `on_callback`, и каждая ветка
достижима. Сверка — по исходнику: реестр врать не должен, но и не обязан знать,
что кто-то собрал `callback_data` руками в обход него.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from b24bot.bot import callbacks, handlers, survey, texts
from b24bot.domain import context

SRC = Path(__file__).resolve().parents[1] / "src" / "b24bot"
HANDLERS = (SRC / "bot" / "handlers.py").read_text(encoding="utf-8")

SOURCES = {path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
           for path in sorted(SRC.rglob("*.py"))}

REGISTERED = {n.ns for n in callbacks.NAMESPACES}


def _emitted() -> dict[str, list[str]]:
    """Префиксы, которые бот действительно шлёт: первый аргумент `cb()`."""
    found: dict[str, list[str]] = {}
    for name, text in SOURCES.items():
        for ns in re.findall(r'\bcb\(\s*"([a-z]+)"', text):
            found.setdefault(ns, []).append(name)
    return found


def _routed() -> list[str]:
    """Префиксы веток `on_callback` — по порядку, с повторами: повтор и есть баг."""
    start = HANDLERS.index("async def on_callback(")
    tail = HANDLERS[start:]
    nxt = re.search(r"^(?:@|async def |def )", tail[1:], re.M)
    body = tail[: nxt.start() + 1] if nxt else tail
    return re.findall(r'\bns == "([a-z]+)"', body)


# --------------------------------------------------------------- главный страж
def test_every_emitted_prefix_is_routed() -> None:
    """Кнопка, которую роутер не знает, выглядит как поломка бота."""
    routed = set(_routed())
    orphans = {ns: files for ns, files in _emitted().items() if ns not in routed}
    assert not orphans, f"префикс отправляется, но не разбирается: {orphans}"


def test_every_branch_is_reachable() -> None:
    """Две ветки на один префикс: вторая недостижима, а Python об этом молчит."""
    routed = _routed()
    dupes = sorted({ns for ns in routed if routed.count(ns) > 1})
    assert not dupes, f"префикс разбирается больше чем одной веткой: {dupes}"


def test_router_and_registry_agree() -> None:
    """Ветка без записи в реестре — тот же разлад, только с другой стороны."""
    assert set(_routed()) == REGISTERED


def test_registry_has_no_dead_namespace() -> None:
    """Объявленный, но никем не отправляемый префикс — ветка, куда никто не придёт."""
    assert set(_emitted()) == REGISTERED


def test_callback_data_is_built_only_by_the_registry() -> None:
    """Ровно одно место собирает `callback_data` — иначе проверки выше слепы.

    Без этого страж проверяет лишь то, что проходит через `cb()`, а мимо него
    можно отправить любой префикс f-строкой: именно так и жили обе стороны бага.
    """
    places = {name for name, text in SOURCES.items() if '"callback_data"' in text}
    assert places == {"bot/keyboards.py"}, f"callback_data собирается мимо реестра: {places}"


def test_every_registered_kind_is_actually_issued() -> None:
    """Реестр описывает живые токены, а не намерения: `kind` обязан где-то выдаваться."""
    issued = set()
    for text in SOURCES.values():
        issued |= set(re.findall(r'issue_token\(\s*"([\w:]+)"', text))
    missing = sorted(n.kind for n in callbacks.NAMESPACES if n.kind not in issued)
    assert not missing, f"вид токена объявлен, но не выдаётся: {missing}"


def test_kind_is_unique_per_namespace() -> None:
    """Пара `ns` ↔ `kind` взаимно однозначна — на этом стоит проверка на нажатии."""
    kinds = [n.kind for n in callbacks.NAMESPACES]
    assert len(kinds) == len(set(kinds)), f"один вид токена на два префикса: {kinds}"


def test_router_checks_the_kind_of_the_token() -> None:
    """Страж по исходнику ловит разлад до деплоя, эта проверка — во время работы."""
    assert 'row["kind"] != callbacks.kind_of(ns)' in HANDLERS


# ------------------------------------------------------------- форма префиксов
def test_prefixes_fit_the_telegram_limit() -> None:
    """`callback_data` — 64 байта; токен занимает 22 символа, плюс двоеточие."""
    for entry in callbacks.NAMESPACES:
        assert re.fullmatch(r"[a-z]{1,3}", entry.ns), entry
        assert len(callbacks.data(entry.ns, "x" * 22).encode()) <= 64, entry


def test_unregistered_prefix_is_refused_loudly() -> None:
    """Опечатка в префиксе — мёртвая кнопка. Пусть падает у нас, а не у человека."""
    with pytest.raises(ValueError, match="не объявлено"):
        callbacks.data("zz", "token")


def test_purpose_is_filled() -> None:
    """Реестр читают, когда разбирают лог: голая буква там бесполезна."""
    for entry in callbacks.NAMESPACES:
        assert entry.purpose.strip(), entry


# ------------------------------------------------------- маршрутизация нажатия
# Страж выше сверяет исходники и ловит разлад до деплоя. Здесь — то же самое
# нажатием: два префикса, которые сталкивались, и попытка отправить токен под
# чужим префиксом. Токены и контекст чата подменяются, до базы дело не доходит.
def _row(kind: str, payload: dict[str, object]) -> dict[str, object]:
    return {"kind": kind, "tenant_id": 1, "owner_tg_id": 77,
            "chat_ref": 5, "payload": payload}


def _ctx() -> context.ChatContext:
    return context.ChatContext(
        chat_ref=5, chat_id=-100500, title="Поддержка", status="active",
        tenant_id=1, is_forum=False,
        projects=[context.ProjectRef(11, 101, "Первый", "Линия Жизни"),
                  context.ProjectRef(12, 102, "Второй", "Линия Жизни")])


def _click(monkeypatch: pytest.MonkeyPatch, data: str,
           row: dict[str, object]) -> list[tuple[str, object]]:
    """Нажатие кнопки. Возвращает журнал того, куда нажатие доехало."""
    trail: list[tuple[str, object]] = []

    async def consume(token: str, actor: int | None) -> dict[str, object]:
        return row

    async def ctx(chat_id: int, thread_id: int | None = None) -> context.ChatContext:
        return _ctx()

    async def linked(tenant_id: int, tg_user_id: int) -> int:
        return 42

    async def do_create(_ctx: object, project: context.ProjectRef, source: object,
                        tg_user_id: int, b24_user_id: int) -> handlers.Reply:
        trail.append(("create", project.id))
        return handlers.Reply("создано")

    async def survey_next(_ctx: object, session: object, items: object,
                          tg_user_id: int) -> handlers.Reply:
        trail.append(("survey", items[0].code))  # type: ignore[index]
        return handlers.Reply("следующий вопрос")

    async def active_for(chat_ref: int, thread_id: int | None,
                         tg_user_id: int) -> survey.Session:
        return survey.Session(id=9, tenant_id=1, chat_ref=5, thread_id=0, owner_tg_id=77,
                              template_id=3, project_id=11, step=0, answers={},
                              last_message_id=None)

    async def questions(tenant_id: int, template_id: int) -> list[survey.Question]:
        return [survey.Question(code="why", text="Что случилось?", required=True,
                                kind="choice", options=[{"value": "v", "label": "Горит"}])]

    async def record(session: object, code: str, value: str) -> None:
        trail.append(("answer", value))

    monkeypatch.setattr(handlers, "consume_token", consume)
    monkeypatch.setattr(handlers, "load_chat_context", ctx)
    monkeypatch.setattr(handlers.access, "linked_b24_user", linked)
    monkeypatch.setattr(handlers, "_do_create", do_create)
    monkeypatch.setattr(handlers, "_survey_next", survey_next)
    monkeypatch.setattr(handlers.survey, "active_for", active_for)
    monkeypatch.setattr(handlers.survey, "questions", questions)
    monkeypatch.setattr(handlers.survey, "record", record)

    click = {"data": data, "from": {"id": 77},
             "message": {"message_id": 1, "chat": {"id": -100500}}}
    reply = asyncio.run(handlers.on_callback({}, click))
    trail.append(("reply", reply.text if reply else None))
    return trail


def test_project_choice_creates_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тот самый баг: в чате с двумя проектами кнопка проекта создаёт задачу."""
    trail = _click(monkeypatch, "tp:token",
                   _row("task:project", {"project_id": 12, "source_message_id": 3}))
    assert ("create", 12) in trail
    assert ("reply", texts.MSG_DIALOG_EXPIRED) not in trail


def test_survey_pick_still_answers_the_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """Опросник от переезда соседа не пострадал: его префикс остался за ним."""
    trail = _click(monkeypatch, "p:token", _row("survey_pick", {"session_id": 9, "index": 0}))
    assert ("answer", "v") in trail
    assert ("survey", "why") in trail


def test_token_under_a_borrowed_prefix_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тот же токен под чужим префиксом уводит нажатие в ветку, ждущую другой payload."""
    trail = _click(monkeypatch, "tp:token", _row("survey_pick", {"session_id": 9, "index": 0}))
    assert trail == [("reply", texts.MSG_DIALOG_EXPIRED)]
