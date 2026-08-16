"""Маппинг полей и лимитер — места, где ошибка не падает, а тихо портит данные."""
from __future__ import annotations

import asyncio
import time

import pytest

from b24bot.b24 import mapping
from b24bot.b24.limiter import BACKGROUND_RESERVE, Lane, PortalLimiter


# ------------------------------------------------------------------- маппинг
@pytest.mark.parametrize("upper,camel", [
    ("ID", "id"),
    ("GROUP_ID", "groupId"),
    ("CREATED_BY", "createdBy"),
    ("UF_TASK_WEBDAV_FILES", "ufTaskWebdavFiles"),
    ("UF_AUTO_80FB9545CBCF", "ufAuto80fb9545cbcf"),
    ("TIME_SPENT_IN_LOGS", "timeSpentInLogs"),
])
def test_to_camel_matches_portal(upper: str, camel: str) -> None:
    """Все пары сняты с живого ответа портала, а не выведены умозрительно."""
    assert mapping.to_camel(upper) == camel


def test_encode_params_flattens_like_bitrix_expects() -> None:
    out = mapping.encode_params({
        "fields": {"TITLE": "Тест", "TAGS": ["a", "b"], "ALLOW_CHANGE_DEADLINE": True},
        "select": ["ID", "TITLE"],
    })
    assert out["fields[TITLE]"] == "Тест"
    assert out["fields[TAGS][0]"] == "a"
    assert out["fields[TAGS][1]"] == "b"
    assert out["fields[ALLOW_CHANGE_DEADLINE]"] == "Y"
    assert out["select[0]"] == "ID"


def test_parse_tags_handles_object_form() -> None:
    """TAGS приходит объектом, ключ — id тега. Список тут был бы ошибкой."""
    raw = {"7": {"id": 7, "title": "devonbot"}, "9": {"id": 9, "title": "idem-abc123"}}
    assert sorted(mapping.parse_tags(raw)) == ["devonbot", "idem-abc123"]
    assert mapping.parse_tags(None) == []
    assert mapping.parse_tags(["x"]) == ["x"]


def test_statuses_are_exactly_five() -> None:
    """Статусов «Новая» (1) и «Отклонена» (7) на портале не существует."""
    assert set(mapping.STATUS_TITLES) == {2, 3, 4, 5, 6}
    assert 1 not in mapping.STATUS_TITLES
    assert 7 not in mapping.STATUS_TITLES
    assert mapping.SUBSTATUS_TITLES[-1] == "Просрочена"


def test_filter_field_aliases_are_singular() -> None:
    """Поля задачи во множественном, поля фильтра — в единственном числе."""
    assert mapping.FILTER_FIELD_ALIASES["ACCOMPLICES"] == "ACCOMPLICE"
    assert mapping.FILTER_FIELD_ALIASES["AUDITORS"] == "AUDITOR"
    assert mapping.FILTER_FIELD_ALIASES["TAGS"] == "TAG"


def test_as_int_survives_string_numbers() -> None:
    assert mapping.as_int("8017") == 8017
    assert mapping.as_int(None) is None
    assert mapping.as_int("") is None
    assert mapping.as_int("не число") is None


def test_allowed_actions_from_action_block() -> None:
    task = {"action": {"complete": True, "defer": False, "start": True}}
    assert mapping.allowed_actions(task) == {"complete", "start"}
    assert mapping.allowed_actions({}) == set()


# ------------------------------------------------------------------- лимитер
@pytest.mark.asyncio
async def test_bucket_allows_burst_then_throttles() -> None:
    """Ведро ёмкостью 50 пропускает всплеск — именно поэтому замер показал 6.4 rps."""
    lim = PortalLimiter(rate=2.0, capacity=5.0)
    started = time.monotonic()
    for _ in range(5):
        await lim.acquire(Lane.INTERACTIVE)
    assert time.monotonic() - started < 0.2, "всплеск не должен ждать"

    started = time.monotonic()
    await lim.acquire(Lane.INTERACTIVE)
    assert time.monotonic() - started >= 0.4, "после всплеска обязан включиться троттлинг"


@pytest.mark.asyncio
async def test_observe_updates_operating_budget() -> None:
    lim = PortalLimiter()
    lim.observe({"operating": 300.0, "operating_reset_at": time.time() + 600})
    assert lim.stats["operating_spent"] == 300.0


@pytest.mark.asyncio
async def test_background_lane_yields_when_operating_is_high() -> None:
    """Фоновая полоса замирает первой, оставляя запас интерактивным запросам."""
    lim = PortalLimiter()
    lim.observe({"operating": lim.operating_limit * (1 - BACKGROUND_RESERVE / 2),
                 "operating_reset_at": time.time() + 600})

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lim.acquire(Lane.BACKGROUND), timeout=0.5)

    # Интерактивная при этом проходит.
    await asyncio.wait_for(lim.acquire(Lane.INTERACTIVE), timeout=0.5)


@pytest.mark.asyncio
async def test_penalize_empties_bucket() -> None:
    lim = PortalLimiter(rate=2.0, capacity=50.0)
    lim.penalize(5.0)
    assert lim.stats["tokens"] == 0.0
