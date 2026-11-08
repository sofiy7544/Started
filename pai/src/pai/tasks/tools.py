"""Инструменты модели для списка задач. Привязаны к одному владельцу."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import FunctionToolset

from pai.tasks.service import TaskError, TaskService, grouped_text


class NewTask(BaseModel):
    text: str = Field(description="что сделать — словами владельца, без смягчений и потери смысла")
    section: str | None = Field(None, description="раздел плана, например «Набор людей», «Техника», «Закупки»")
    person: str | None = Field(None, description="кто делает или с кем (имя/прозвище), если сказано")
    due: str | None = Field(None, description="срок, если сказан («завтра», «до пятницы», «2026-11-10»)")


def build_toolset(service: TaskService, user_id: int, changed: list[str]) -> FunctionToolset:
    async def add_tasks(tasks: list[NewTask]) -> str:
        """Добавить задачи в список задач владельца (план работ). Можно сразу несколько.

        Args:
            tasks: задачи; каждая — отдельный пункт плана
        """
        lines = []
        for t in tasks[:50]:
            try:
                v = await service.add(user_id, t.text, section=t.section, person=t.person, due=t.due)
            except TaskError as exc:
                lines.append(f"Не добавлено «{t.text[:60]}»: {exc}")
                continue
            lines.append(f"Добавлено #{v.id} [{v.section}] {v.text}")
            changed.append(f"#{v.id}")
        return "\n".join(lines) or "Нечего добавлять."

    async def list_tasks(include_closed: bool = False) -> str:
        """Показать задачи владельца по разделам (с номерами). Вызывай перед тем, как говорить о плане,
        что сделано или что осталось, — не полагайся на память переписки.

        Args:
            include_closed: показать и выполненные/отменённые
        """
        items = await service.list(user_id, include_closed=include_closed)
        return grouped_text(items) if items else "Задач нет."

    async def update_task(task_id: int, text: str | None = None, section: str | None = None,
                          person: str | None = None, due: str | None = None,
                          status: Literal["open", "done", "cancelled"] | None = None) -> str:
        """Изменить задачу или отметить её: status=done — сделано, cancelled — отменено, open — вернуть.

        Args:
            task_id: номер задачи (из list_tasks)
            text: новый текст
            section: новый раздел
            person: кто / с кем
            due: срок
            status: open | done | cancelled
        """
        try:
            v = await service.update(user_id, task_id, text=text, section=section, person=person, due=due,
                                     status=status)
        except TaskError as exc:
            return f"Не изменено: {exc}"
        changed.append(f"#{v.id}")
        return f"Изменено: {v.line()}"

    return FunctionToolset([add_tasks, list_tasks, update_task])


INSTRUCTIONS = """\
## Список задач (план работ владельца)
Инструменты: add_tasks, list_tasks, update_task. Список хранится в базе и не теряется, в отличие от переписки.
- Владелец диктует задачи, план, «добавь задачу», «не забудь…», «запиши» — добавь через add_tasks, разложив по разделам.
- Записывай смысл точно, как сказал владелец: не смягчай, не меняй условия и формулировки по сути
  (например «условия — хуже не будет» нельзя превращать в «уточнить условия»). Грубые слова можно опустить,
  смысл — нет. Неясное — запиши как есть и коротко переспроси.
- Перед ответом о плане, о том, что сделано или осталось, — вызови list_tasks.
- «Сделал …» — update_task со status=done.
- Не говори «записал», «добавил», «зафиксировал в плане», если add_tasks/update_task не вернули «Добавлено»/«Изменено».
- Полный список владелец видит командой /tasks, там же — выгрузка в Excel."""
