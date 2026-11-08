"""Список задач: сервис, инструменты модели, /tasks и выгрузка в Excel."""

from __future__ import annotations

import io

import pytest

from pai.bot import texts
from pai.tasks.service import TaskError
from tests.conftest import OWNER
from tests.test_media_bot import mbot, tg, voice  # noqa: F401 — фикстуры бота


async def test_service_add_list_update_and_isolation(assistant) -> None:
    svc = assistant.tasks
    owner = await assistant.get_user(OWNER)
    other = await assistant.get_user(222)
    a = await svc.add(owner.id, "Позвонить Гайдару по работе", section="Набор людей", person="Гайдар")
    await svc.add(owner.id, "Купить хромакей", section="Закупки")
    assert [(v.section, v.text) for v in await svc.list(owner.id)] == [
        ("Закупки", "Купить хромакей"), ("Набор людей", "Позвонить Гайдару по работе")]  # по разделам
    done = await svc.update(owner.id, a.id, status="done")
    assert done.status == "done" and [v.text for v in await svc.list(owner.id)] == ["Купить хромакей"]
    assert len(await svc.list(owner.id, include_closed=True)) == 2
    with pytest.raises(TaskError):
        await svc.update(other.id, a.id, status="open")  # чужую задачу не изменить
    assert await svc.list(other.id) == []


async def test_model_adds_tasks_and_bot_shows_and_exports(mbot, script) -> None:  # noqa: F811
    send, tg, assistant, _ = mbot  # noqa: F811
    script.replies = [
        {"tool": "add_tasks", "args": {"tasks": [
            {"text": "Предложить АК работу: условия — хуже не будет; вести команду", "section": "Набор людей", "person": "АК"},
            {"text": "Купить хромакей", "section": "Закупки"}]}},
        "Добавил 2 задачи.",
    ]
    await send(lambda: tg.push_text(OWNER, "запиши: АК предложить работу, условия хуже не будет; купить хромакей"))
    assert "add_tasks" in script.seen[-1].instructions or "Список задач" in script.seen[-1].instructions
    calls = await send(lambda: tg.push_text(OWNER, texts.BTN_TASKS))
    shown = "\n".join(p.get("text", "") for m, p in calls if m == "sendMessage")
    assert "📌 Набор людей" in shown and "условия — хуже не будет" in shown and "📌 Закупки" in shown
    calls = await send(lambda: tg.push_callback(OWNER, "tasks:xlsx"))
    doc = next(p for m, p in calls if m == "sendDocument")
    sent = doc[doc["document"].removeprefix("attach://")]
    assert sent.filename == "Задачи.xlsx" and sent.data.startswith(b"PK")


async def test_excel_export_content(assistant) -> None:
    from openpyxl import load_workbook

    from pai.media.outputs import build_file
    from pai.tasks.service import to_csv_table

    owner = await assistant.get_user(OWNER)
    await assistant.tasks.add(owner.id, "Поговорить с Тайсоном; аудит бывших сотрудников", section="Тайсон", due="на неделе")
    f = build_file("Задачи.xlsx", to_csv_table(await assistant.tasks.list(owner.id, include_closed=True)))
    ws = load_workbook(io.BytesIO(f.data)).active
    assert [c.value for c in ws[1]] == ["№", "Раздел", "Задача", "Кто / с кем", "Срок", "Статус"]
    assert ws["C2"].value.startswith("Поговорить с Тайсоном") and ws["E2"].value == "на неделе"
