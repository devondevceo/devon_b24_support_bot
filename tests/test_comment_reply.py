"""`/comment` ответом на сообщение.

Комментарий чаще всего повторяет то, что человек уже написал в чате. Пока текст
приходилось набирать заново, вместе с ним терялись и файлы этого сообщения.
Теперь `/comment 233` реплаем берёт и текст, и вложения того сообщения,
на которое ответили.

Отдельная забота — подпись. Текст в задачу уехал чужой, и в комментарии обязано
быть видно, чей именно: иначе в задаче окажется реплика одного человека за
подписью другого.
"""
from __future__ import annotations

from b24bot.bot.handlers import comment_input

ALICE = {"id": 1, "first_name": "Сергей", "last_name": "Крищунс", "username": "skr"}
BOB = {"id": 2, "first_name": "Роман", "username": "rd"}
PHOTO = [{"file_id": "AgACphoto", "file_size": 90}]
DOC = {"file_id": "BQACdoc", "file_name": "смета.pdf", "file_size": 1024}


def _msg(**kw: object) -> dict[str, object]:
    return {"message_id": 10, "from": BOB, **kw}


def test_reply_without_text_takes_the_quoted_message() -> None:
    data = comment_input("233", _msg(reply_to_message={
        "message_id": 9, "from": ALICE, "text": "у клиента не грузится форма"}))
    assert data.task_id == 233
    assert data.text == "у клиента не грузится форма"
    assert data.quoted_author == "Сергей Крищунс (@skr)"


def test_own_text_wins_over_the_quoted_one() -> None:
    """Написал своё — значит, хотел своё: чужую реплику подставлять нельзя."""
    data = comment_input("233 проверил, дело в кэше", _msg(reply_to_message={
        "message_id": 9, "from": ALICE, "text": "у клиента не грузится форма"}))
    assert data.text == "проверил, дело в кэше"
    assert data.quoted_author == "", "подпись о цитате тут была бы неправдой"


def test_files_of_the_quoted_message_come_along() -> None:
    """Половина смысла сообщения часто лежит во вложении."""
    data = comment_input("233", _msg(reply_to_message={
        "message_id": 9, "from": ALICE, "text": "вот скриншот", "photo": PHOTO}))
    assert [a.file_id for a in data.attachments] == ["AgACphoto"]


def test_files_of_both_messages_come_along_without_duplicates() -> None:
    data = comment_input("233", _msg(document=DOC, reply_to_message={
        "message_id": 9, "from": ALICE, "text": "смотри", "document": DOC,
        "photo": PHOTO}))
    assert [a.file_id for a in data.attachments] == ["BQACdoc", "AgACphoto"]


def test_caption_counts_as_text() -> None:
    """Подпись под фото — такое же сообщение, как и обычный текст."""
    data = comment_input("233", _msg(reply_to_message={
        "message_id": 9, "from": ALICE, "caption": "форма после правки",
        "photo": PHOTO}))
    assert data.text == "форма после правки"


def test_reply_with_files_only_still_has_something_to_say() -> None:
    """Текста нет, но вложения есть — команда обязана сработать, а не отказать."""
    data = comment_input("233", _msg(reply_to_message={
        "message_id": 9, "from": ALICE, "photo": PHOTO}))
    assert data.text == ""
    assert data.attachments, "иначе обработчик ответит подсказкой по формату"
    assert data.quoted_author == "Сергей Крищунс (@skr)"


def test_service_message_of_a_forum_topic_is_not_a_quote() -> None:
    """В форуме Telegram сам подставляет ответ на сообщение о создании топика.

    Приняв его за цитату, бот приписал бы комментарий тому, кто завёл топик,
    и не сказал бы ни слова о содержимом.
    """
    data = comment_input("233", _msg(message_thread_id=7, reply_to_message={
        "message_id": 1, "from": ALICE, "is_topic_message": True,
        "forum_topic_created": {"name": "Линия Жизни"}}))
    assert data.quoted_author == ""
    assert data.text == ""


def test_command_without_a_number_is_not_guessed() -> None:
    """Номер обязателен: угадывать задачу по контексту — значит писать не туда."""
    assert comment_input("", _msg()).task_id is None
    assert comment_input("текст без номера", _msg()).task_id is None


def test_portal_link_works_as_the_number() -> None:
    """Номер копируют не только числом, но и адресом из браузера."""
    data = comment_input(
        "https://devondev.bitrix24.ru/company/personal/user/7/tasks/task/view/233/ ага",
        _msg())
    assert data.task_id == 233
    assert data.text == "ага"


def test_nothing_to_say_and_nothing_to_attach() -> None:
    data = comment_input("233", _msg())
    assert not data.text and not data.attachments
