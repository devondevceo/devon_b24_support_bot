"""Опросник с привязкой ответов к полям задачи.

Главное правило, ради которого написана половина этих тестов: **ответ человека
не теряется никогда**. Не подошёл полю — уходит в тело задачи вместе с честной
строкой о том, что разобрать не удалось.
"""
from __future__ import annotations

import pytest

from b24bot.api.app_survey import parse_options
from b24bot.b24 import fields as b24_fields
from b24bot.bot.survey import Question, assemble


# ------------------------------------------------------- список полей портала
def test_enum_values_come_in_two_shapes() -> None:
    """PRIORITY отдаёт словарь, DURATION_TYPE — плоский список.

    На списке разбор падал `'list' object has no attribute 'items'` — поймано
    на живом портале, см. docs/00-portal-facts.md §13.1.
    """
    assert b24_fields._values_of({"2": "Высокий"}) == {"2": "Высокий"}
    assert b24_fields._values_of(["secs", "days"]) == {"secs": "secs", "days": "days"}
    assert b24_fields._values_of(None) == {}


# ------------------------------------------------------------- преобразование
@pytest.mark.parametrize(("text", "expect"), [
    ("завтра", "T18:00:00+03:00"),
    ("20.08.2026", "2026-08-20T18:00:00+03:00"),
    ("20.08.2026 14:30", "2026-08-20T14:30:00+03:00"),
    ("20.08", "-08-20T18:00:00+03:00"),
    ("через 2 дня", "T18:00:00+03:00"),
    ("через 1 неделю", "T18:00:00+03:00"),
    ("3 сентября", "-09-03T18:00:00+03:00"),
])
def test_human_dates_are_understood(text: str, expect: str) -> None:
    """Люди пишут «завтра», а не ISO. Смещение обязательно и явное: у каждого
    портала свой часовой пояс, голая дата уезжает на сутки."""
    assert expect in b24_fields.to_b24("datetime", text)


@pytest.mark.parametrize("text", ["когда-нибудь", "31.02.2026", "asap", ""])
def test_impossible_dates_are_refused_not_guessed(text: str) -> None:
    """Догадка тут хуже отказа: неверный срок в задаче выглядит как настоящий."""
    with pytest.raises(b24_fields.ConversionError):
        b24_fields.to_b24("datetime", text)


def test_integer_survives_units_written_by_hand() -> None:
    assert b24_fields.to_b24("integer", "3600") == 3600
    assert b24_fields.to_b24("integer", "около 120 минут") == 120
    with pytest.raises(b24_fields.ConversionError):
        b24_fields.to_b24("integer", "много")


def test_tags_field_always_becomes_a_list() -> None:
    assert b24_fields.to_b24("array", "срочно") == ["срочно"]


# -------------------------------------------------------------- разбор ответов
def _q(code: str, text: str, **kw: object) -> Question:
    return Question(code=code, text=text, required=False, **kw)  # type: ignore[arg-type]


def test_linked_answers_go_to_fields_others_to_body() -> None:
    items = [
        _q("what", "Что случилось?"),
        _q("prio", "Насколько срочно?", kind="choice", b24_field="PRIORITY",
           b24_field_type="enum",
           options=[{"label": "Горит", "value": "2"}, {"label": "Обычное", "value": "1"}]),
        _q("when", "Когда нужно?", b24_field="DEADLINE", b24_field_type="datetime"),
    ]
    built = assemble(items, {"what": "Не открывается форма", "prio": "2",
                             "when": "20.08.2026"})

    assert built.fields["PRIORITY"] == "2"
    assert built.fields["DEADLINE"].startswith("2026-08-20T")
    assert built.title == "Не открывается форма"
    assert "Не открывается форма" in built.description
    assert built.rejected == []


def test_choice_shows_label_in_body_not_the_raw_value() -> None:
    """«Высокий» в описании полезен, «2» — мусор."""
    items = [_q("prio", "Срочность", kind="choice", b24_field="PRIORITY",
                b24_field_type="enum",
                options=[{"label": "Горит", "value": "2"}])]
    built = assemble(items, {"prio": "2"})
    assert "Горит" in built.description
    assert "\n2" not in built.description


def test_unparsable_answer_lands_in_body_and_is_named() -> None:
    """Человек это написал. Молча выбросить нельзя — он считает, что его услышали."""
    items = [_q("when", "Когда нужно?", b24_field="DEADLINE",
                b24_field_type="datetime")]
    built = assemble(items, {"when": "когда-нибудь"})

    assert "DEADLINE" not in built.fields
    assert "когда-нибудь" in built.description
    assert built.rejected and "Когда нужно?" in built.rejected[0]
    assert "Не удалось разобрать" in built.description


def test_title_can_be_taken_from_a_linked_question() -> None:
    items = [_q("subj", "Тема обращения", b24_field="TITLE", b24_field_type="string"),
             _q("body", "Подробности")]
    built = assemble(items, {"subj": "Сломался вход", "body": "Не пускает с утра"})

    assert built.title == "Сломался вход"
    assert "TITLE" not in built.fields          # уходит через draft.title, не полем
    assert "Не пускает с утра" in built.description


def test_tags_from_answers_accumulate() -> None:
    items = [_q("a", "Тег 1", b24_field="TAGS", b24_field_type="array"),
             _q("b", "Тег 2", b24_field="TAGS", b24_field_type="array")]
    built = assemble(items, {"a": "оплата", "b": "срочно"})
    assert built.fields["TAGS"] == ["оплата", "срочно"]


def test_brackets_in_answers_cannot_forge_bbcode() -> None:
    """И-6 без исключений: ответ человека — такая же подстановка, как всё прочее."""
    items = [_q("x", "Опишите проблему")]
    built = assemble(items, {"x": "[b]жирный[/b] и [url=http://evil]ссылка[/url]"})
    assert "[b]жирный" not in built.description
    assert "[url=" not in built.description


def test_empty_answers_do_not_create_empty_sections() -> None:
    items = [_q("a", "Первый"), _q("b", "Второй")]
    built = assemble(items, {"a": "  ", "b": "есть ответ"})
    assert "Первый" not in built.description
    assert built.title == "есть ответ"


# ----------------------------------------------------------- варианты ответов
def test_options_parse_label_and_value() -> None:
    parsed = parse_options("Горит = 2\nОбычное = 1\n\nМожно позже=0")
    assert parsed == [{"label": "Горит", "value": "2"},
                      {"label": "Обычное", "value": "1"},
                      {"label": "Можно позже", "value": "0"}]


def test_option_without_equals_uses_label_as_value() -> None:
    """Для строкового поля подпись и есть значение — знак равенства не нужен."""
    assert parse_options("Инцидент") == [{"label": "Инцидент", "value": "Инцидент"}]


def test_options_are_capped() -> None:
    """Двадцать кнопок под вопросом — уже неюзабельно, больше просто не влезет."""
    parsed = parse_options("\n".join(f"вариант {i}" for i in range(50)))
    assert len(parsed) == 20


def test_label_for_falls_back_to_the_raw_value() -> None:
    """Набор правили, пока человек отвечал: подписи для значения больше нет."""
    q = _q("x", "Срочность", kind="choice",
           options=[{"label": "Горит", "value": "2"}])
    assert q.label_for("2") == "Горит"
    assert q.label_for("9") == "9"


def test_choice_question_asks_for_a_button_not_a_reply() -> None:
    """Просить реплай там, где есть кнопки, — значит звать печатать мимо подписи."""
    from b24bot.bot.survey import question_text

    choice = _q("prio", "Срочность?", kind="choice",
                options=[{"label": "Горит", "value": "2"}])
    assert "кнопкой ниже" in question_text(choice, 0, 3)
    assert "Ответьте на это сообщение" in question_text(_q("x", "Что случилось?"), 0, 3)


# ------------------------------------------------------- создание своего поля
def test_field_name_is_transliterated_and_prefixed() -> None:
    """Битрикс принимает только [A-Z0-9_], а подпись поля человек пишет по-русски."""
    from b24bot.b24.fields import field_name_for

    assert field_name_for("Срочность", set()) == "UF_SD_SROCHNOST"
    assert field_name_for("Тип обращения!", set()) == "UF_SD_TIP_OBRASCHENIYA"
    assert field_name_for("", set()) == "UF_SD_FIELD"


def test_field_name_does_not_collide() -> None:
    """Имя занято — берём следующее, а не падаем: Битрикс на дубль отвечает ERROR_CORE."""
    from b24bot.b24.fields import field_name_for

    assert field_name_for("Срочность", {"UF_SD_SROCHNOST"}) == "UF_SD_SROCHNOST_2"
    assert field_name_for(
        "Срочность", {"UF_SD_SROCHNOST", "UF_SD_SROCHNOST_2"}) == "UF_SD_SROCHNOST_3"


def test_enum_items_maps_label_to_element_id() -> None:
    """В enum-поле уходит ID элемента. Подписью Битрикс молча пишет 0."""
    from b24bot.b24.fields import enum_items

    meta = {"LIST": [{"ID": "325", "VALUE": "Горит"},
                     {"ID": "327", "VALUE": "Обычное"}]}
    assert enum_items(meta) == {"Горит": "325", "Обычное": "327"}
    assert enum_items({}) == {}
