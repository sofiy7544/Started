"""Вложения в Telegram целиком: настоящий aiogram, фейковый Bot API по HTTP, подставная модель
(FunctionModel) — видно, что именно получает модель и что уходит владельцу."""

from __future__ import annotations

import json

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from sqlalchemy import select

from pai.bot import texts
from pai.bot.app import AppContext, build_dispatcher
from pai.db.models import Turn, UsageRecord
from pai.services.assistant import Outcome
from tests import media_samples as ms
from tests.conftest import OWNER, STRANGER, make_audio
from tests.fakes import FakeTelegram
from tests.test_voice import FakeSTT, FakeTTS

FAST = {"media_wait_seconds": 0.5, "media_text_wait_seconds": 0.1}


@pytest.fixture
async def tg():
    fake = FakeTelegram()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def voice(settings, sessions):
    from pai.services.budget import BudgetGuard
    from pai.voice.engine import VoiceEngine

    return VoiceEngine(settings=settings, budget=BudgetGuard(sessions, monthly_limit_usd=10), stt=FakeSTT(), tts=FakeTTS())


@pytest.fixture
async def mbot(tg, make_assistant, sessions, voice):
    assistant = make_assistant(media=True, voice=voice, settings_override=FAST)
    s = assistant._settings
    bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    dp = build_dispatcher(AppContext(s, assistant, sessions, voice, assistant.media))

    async def send(*pushes) -> list[tuple[str, dict]]:
        """Отправить одно или несколько сообщений подряд и дождаться ответа."""
        before = len(tg.calls)
        for push in pushes:
            push()
            await dp.feed_raw_update(bot, tg.updates.get_nowait())
        await dp["collector"].wait_idle()
        await assistant.memory.wait()
        return tg.calls[before:]

    yield send, tg, assistant, voice
    await bot.session.close()


def out_texts(calls) -> list[str]:
    return [p.get("text") or p.get("caption", "") for m, p in calls if m in ("sendMessage", "sendVoice")]


def images_in(messages) -> list[BinaryContent]:
    found = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart) and not isinstance(p.content, str):
                    found += [c for c in p.content if isinstance(c, BinaryContent)]
    return found


def prompt_text(messages) -> str:
    out = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    out += [p.content] if isinstance(p.content, str) else [c for c in p.content if isinstance(c, str)]
                if isinstance(p, ToolReturnPart):
                    out.append(str(p.content))
    return "\n".join(out)


async def test_photo_with_caption_is_shown_to_model_and_answered(mbot, script, sessions) -> None:
    send, tg, *_ = mbot
    script.replies = ["На чеке итог 980 грн."]
    calls = await send(lambda: tg.push_file(OWNER, "photo", ms.image_with_text("ИТОГО 980 грн"), caption="Сколько итог?"))
    assert out_texts(calls) == ["На чеке итог 980 грн."]  # одиночное фото — без отчёта о получении
    seen = script.seen[-1]
    imgs = images_in(seen.messages)
    assert len(imgs) == 1 and imgs[0].media_type == "image/jpeg" and imgs[0].identifier == "att-1"
    assert "Сколько итог?" in prompt_text(seen.messages)
    assert "Опись вложений" in seen.instructions and "#1 изображение" in seen.instructions
    async with sessions() as s:
        turn = (await s.scalars(select(Turn))).one()
    assert "Сколько итог?" in turn.user_text and "#1 изображение" in turn.user_text
    assert b"/9j" not in turn.messages  # сама картинка в историю не попала


async def test_photo_then_voice_then_text_are_one_task_and_linked(mbot, script, voice) -> None:
    send, tg, _a, eng = mbot
    eng.stt.text = "Здесь ошибка в расчёте, найди её"
    script.replies = ["Ошибка в строке 2."]
    calls = await send(
        lambda: tg.push_file(OWNER, "photo", ms.image_with_text("2+2=5")),
        lambda: tg.push_voice(OWNER, make_audio(1.0), duration=1),
    )
    assert len(script.seen) == 1  # фото + голосовое — один запрос к модели
    first = script.seen[-1]
    assert len(images_in(first.messages)) == 1
    assert "Здесь ошибка в расчёте" in prompt_text(first.messages)
    assert texts.heard("Здесь ошибка в расчёте, найди её") in out_texts(calls)
    assert any(m == "sendVoice" for m, _ in calls)  # спросили голосом — ответ и голосом
    # Позже: «а теперь сравни с предыдущим» — модель сама открывает прошлое фото инструментом.
    script.replies = ["Ещё одно фото пришлите", {"tool": "view_attachment", "args": {"attachment_id": 1}},
                      "Сравнила: на новом фото ошибки нет."]
    await send(lambda: tg.push_text(OWNER, "просто вопрос"))
    calls = await send(lambda: tg.push_file(OWNER, "photo", ms.image_with_text("2+2=4"), caption="А теперь сравни с предыдущим"))
    final = script.seen[-1]
    assert "#1 изображение" in final.instructions and "#2 изображение" in final.instructions
    assert len(images_in(final.messages)) == 2  # новое фото + открытое инструментом старое
    assert out_texts(calls)[-1] == "Сравнила: на новом фото ошибки нет."


async def test_album_and_question_after_it(mbot, script) -> None:
    send, tg, *_ = mbot
    script.replies = ["Отличаются цветом фона."]
    calls = await send(
        lambda: tg.push_file(OWNER, "photo", ms.image_with_text("A", color="white"), media_group="g1"),
        lambda: tg.push_file(OWNER, "photo", ms.image_with_text("A", color="yellow"), media_group="g1"),
        lambda: tg.push_text(OWNER, "Найди различия"),
    )
    assert len(script.seen) == 1 and len(images_in(script.seen[-1].messages)) == 2
    out = out_texts(calls)
    assert out[0].startswith("📎 Получено: 2 (полностью 2)") and out[-1] == "Отличаются цветом фона."


async def test_documents_are_read_locally_with_receipt(mbot, script) -> None:
    send, tg, *_ = mbot
    script.replies = ["Сумма в счёте — 980."]
    calls = await send(
        lambda: tg.push_file(OWNER, "document", ms.pdf_with_text([["Invoice 4521", "Total 980 UAH"]]), name="invoice.pdf"),
        lambda: tg.push_file(OWNER, "document", ms.xlsx_file({"Оплаты": [["Счёт", "Сумма"], [4521, 950]]}), name="pay.xlsx",
                             caption="Сверь счёт с оплатами"),
    )
    out = out_texts(calls)
    assert "📎 Получено: 2" in out[0] and "invoice.pdf — 1 стр.; текст прочитан" in out[0] and "pay.xlsx" in out[0]
    text = prompt_text(script.seen[-1].messages)
    assert "Invoice 4521" in text and "4521 | 950" in text and "Сверь счёт с оплатами" in text
    assert images_in(script.seen[-1].messages) == []  # текстовые документы — без картинок, дёшево


async def test_scanned_pdf_pages_go_to_model_as_images(mbot, script) -> None:
    send, tg, *_ = mbot
    script.replies = ["На скане: Счёт 777."]
    calls = await send(lambda: tg.push_file(OWNER, "document", ms.scanned_pdf("Счёт 777"), name="scan.pdf",
                                            caption="Прочитай"))
    assert "текст сканов читаю по изображению страниц" in out_texts(calls)[0]
    imgs = images_in(script.seen[-1].messages)
    assert len(imgs) == 1 and imgs[0].identifier == "att-1-p1"
    assert "стр. 1 (скан" in prompt_text(script.seen[-1].messages)


async def test_big_document_parts_search_and_digest(mbot, script, sessions) -> None:
    send, tg, assistant, _ = mbot
    assistant._settings = assistant._settings.model_copy(update={"media_inline_chars": 3000})
    assistant.media.context._settings = assistant._settings
    paragraphs = [f"Раздел {i}. " + "Обычный текст договора. " * 40 for i in range(1, 15)]
    paragraphs[11] += " Штраф за просрочку составляет 7 процентов."
    script.replies = [
        {"tool": "search_attachments", "args": {"query": "штраф просрочку"}},
        {"tool": "read_attachment", "args": {"attachment_id": 1, "part": 2}},
        {"tool": "digest_document", "args": {"attachment_id": 1}},
        "Штраф — 7%.",
    ]
    await send(lambda: tg.push_file(OWNER, "document", ms.docx_file("Договор", paragraphs), name="contract.docx",
                                    caption="Какой штраф?"))
    first_request = script.seen[0]
    assert "Документ большой" in prompt_text(first_request.messages)
    # Конспект частей делает модель отдельными запросами; результаты кэшируются.
    digest_calls = [s for s in script.seen if "Сделай плотный конспект" in (s.instructions or "")]
    final = script.seen[-1]
    text = prompt_text(final.messages)
    assert "Штраф за просрочку составляет 7 процентов" in text  # нашёлся поиском
    assert "часть 2 из" in text  # прочитана часть
    assert digest_calls and "[часть 1 —" in text
    async with sessions() as s:
        ops = {r.operation for r in await s.scalars(select(UsageRecord))}
    assert {"chat", "digest"} <= ops


async def test_create_file_tool_sends_document(mbot, script) -> None:
    send, tg, *_ = mbot
    script.replies = [{"tool": "create_file", "args": {"filename": "итог.xlsx", "content": "Товар;Сумма\nЧай;12"}},
                      "Готово, таблица во вложении."]
    calls = await send(lambda: tg.push_file(OWNER, "document", b"name,sum\ntea,12\n", name="data.csv",
                                            caption="Сделай из этого Excel"))
    docs = [p for m, p in calls if m == "sendDocument"]
    assert len(docs) == 1
    sent = docs[0][docs[0]["document"].removeprefix("attach://")]  # aiogram шлёт файл отдельной частью
    assert sent.filename == "итог.xlsx" and sent.data.startswith(b"PK")
    assert out_texts(calls)[-1] == "Готово, таблица во вложении."


async def test_model_claiming_file_without_tool_sends_nothing(mbot, script) -> None:
    send, tg, *_ = mbot
    script.replies = ["Файл создан!"]
    calls = await send(lambda: tg.push_file(OWNER, "document", b"a,b\n1,2\n", name="d.csv", caption="в эксель"))
    assert not [m for m, _ in calls if m == "sendDocument"]


async def test_video_frames_and_speech(mbot, script, voice) -> None:
    send, tg, _a, eng = mbot
    eng.stt.text = "Привет, это тестовое видео"
    script.replies = ["В видео меняются надписи, человек здоровается."]
    calls = await send(lambda: tg.push_file(OWNER, "video", ms.video_file(seconds=6), caption="Что в видео?"))
    receipt = out_texts(calls)[0]
    assert "🎬" in receipt and "длительность 00:06" in receipt and "речь распознана" in receipt
    seen = script.seen[-1]
    imgs = images_in(seen.messages)
    assert 3 <= len(imgs) <= 10
    text = prompt_text(seen.messages)
    assert "кадр 00:0" in text and "Привет, это тестовое видео" in text and "00:00–00:06" in text
    # Конкретный момент — кадры отрезка по запросу модели.
    script.replies = [{"tool": "video_frames", "args": {"attachment_id": 1, "start_seconds": 4, "end_seconds": 5, "count": 2}},
                      "На 4-й секунде надпись «сек 4»."]
    await send(lambda: tg.push_text(OWNER, "что на 4-й секунде?"))
    assert len(images_in(script.seen[-1].messages)) == 2


async def test_audio_file_is_transcribed_as_material(mbot, script, voice) -> None:
    send, tg, _a, eng = mbot
    eng.stt.text = "Совещание: запуск переносим на апрель"
    script.replies = ["Итог: запуск в апреле."]
    calls = await send(lambda: tg.push_file(OWNER, "audio", make_audio(3.0, "mp3"), name="meeting.mp3",
                                            caption="Сделай краткий итог"))
    assert "meeting.mp3" in out_texts(calls)[0]
    assert "Совещание: запуск переносим на апрель" in prompt_text(script.seen[-1].messages)
    assert eng.stt.calls == 1


async def test_dangerous_big_and_unknown_files(mbot, script, tg) -> None:
    send, *_ = mbot
    calls = await send(
        lambda: tg.push_file(OWNER, "document", b"MZ" + b"\x00" * 200, name="invoice.pdf"),
        lambda: tg.push_file(OWNER, "document", b"x", name="setup.exe"),
        lambda: tg.push_file(OWNER, "document", b"x" * 100, name="huge.mp4", size=50 * 1024 * 1024),
    )
    receipt = out_texts(calls)[0]
    assert "не удалось 3" in receipt
    assert "invoice.pdf — исполняемые файлы" in receipt and "setup.exe — исполняемые" in receipt
    assert "huge.mp4 — больше 20 МБ" in receipt
    assert script.seen == []  # ни вопроса, ни годных файлов — модель не вызывалась
    assert "f3" not in [p.get("file_id") for m, p in tg.calls if m == "getFile"]  # большой файл не скачивали


async def test_zip_with_documents_and_images(mbot, script) -> None:
    send, tg, *_ = mbot
    script.replies = ["В архиве план и схема."]
    data = ms.zip_file({"plan.txt": "План: запуск 1 марта".encode(), "scheme.png": ms.image_with_text("схема", fmt="PNG"),
                        "run.bat": b"echo hi"})
    calls = await send(lambda: tg.push_file(OWNER, "document", data, name="project.zip", caption="Что тут?"))
    receipt = out_texts(calls)[0]
    assert "project.zip — файлов: 3, разобрано: 2" in receipt and "run.bat" in receipt
    assert "project.zip/plan.txt" in receipt and "project.zip/scheme.png" in receipt
    assert len(images_in(script.seen[-1].messages)) == 1 and "запуск 1 марта" in prompt_text(script.seen[-1].messages)


async def test_stranger_files_are_not_processed(mbot, script, tg) -> None:
    send, *_ = mbot
    calls = await send(lambda: tg.push_file(STRANGER, "photo", ms.image_with_text("x")))
    assert out_texts(calls) == [texts.ACCESS_DENIED]
    assert not [m for m, _ in calls if m == "getFile"] and script.seen == []


async def test_vision_routing(make_assistant, script) -> None:
    from pai.media.context import PreparedMedia

    media = PreparedMedia(parts=["фото", BinaryContent(b"\xff\xd8\xff", media_type="image/jpeg")], image_count=1)
    local = make_assistant(media=True, settings_override={"ollama_base_url": "http://localhost:11434/v1"})
    user = await local.get_user(OWNER)
    await local.set_model(user, "ollama-local")
    answer = await local.ask(user, "что на фото?", media)
    assert answer.outcome is Outcome.OK and answer.model.vision  # локальная не видит — ответила облачная
    only_local = make_assistant(media=True, settings_override={"ollama_base_url": "http://x/v1", "openai_api_key": None})
    user2 = await only_local.get_user(OWNER)
    answer = await only_local.ask(user2, "что на фото?", media)
    assert answer.outcome is Outcome.FAILED and answer.text == texts.NO_VISION_MODEL


async def test_other_users_attachment_is_not_reachable(mbot, script, make_assistant) -> None:
    send, tg, assistant, _ = mbot
    script.replies = ["ок"]
    await send(lambda: tg.push_file(OWNER, "photo", ms.image_with_text("секрет")))
    other = await assistant.get_user(555)
    from pai.media.tools import FileEffects, build_toolset

    toolset = build_toolset(assistant.media, other.id, 999, FileEffects())
    view = toolset.tools["view_attachment"].function
    assert "не найдено" in await view(1)


async def test_text_without_files_is_unchanged(mbot, script) -> None:
    send, tg, *_ = mbot
    script.replies = ["Привет!"]
    calls = await send(lambda: tg.push_text(OWNER, "привет"))
    assert out_texts(calls) == ["Привет!"]
    assert "Опись вложений" not in script.seen[-1].instructions  # без вложений инструкции прежние
    _ = json


async def test_full_wipe_removes_attachments_and_files(mbot, script, sessions) -> None:
    send, tg, assistant, _ = mbot
    await send(lambda: tg.push_file(OWNER, "photo", ms.image_with_text("личное")))
    user = await assistant.get_user(OWNER)
    folder = assistant.media.store.media_dir / f"u{user.id}"
    assert folder.exists()
    await assistant.wipe_user(user)
    assert not folder.exists()
    from pai.db.models import Attachment

    async with sessions() as s:
        assert (await s.scalars(select(Attachment))).all() == []


async def test_long_voice_is_transcribed_in_parts_even_without_duration(mbot, script) -> None:
    send, tg, _a, eng = mbot
    eng.stt.text = "часть"
    script.replies = ["Понял задачу."]
    await send(lambda: tg.push_voice(OWNER, make_audio(170.0), duration=0))  # Telegram не указал длительность
    assert eng.stt.calls == 2  # 170 с → две части по 2 минуты
    assert script.seen[-1].user_prompts[-1] == "часть часть"
