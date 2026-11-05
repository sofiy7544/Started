"""Отправка сообщений по инициативе бота (напоминания, инициатива) через Telegram."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from pai.reminders.delivery import DeliveryError
from pai.reminders.service import Due


def reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово", callback_data=f"rem:done:{reminder_id}")],
        [InlineKeyboardButton(text="⏰ 15 мин", callback_data=f"rem:snz:{reminder_id}:15"),
         InlineKeyboardButton(text="⏰ 1 час", callback_data=f"rem:snz:{reminder_id}:60"),
         InlineKeyboardButton(text="⏰ Завтра", callback_data=f"rem:snz:{reminder_id}:1440")],
    ])


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def _send(self, tg_id: int, text: str, markup=None) -> int:  # type: ignore[no-untyped-def]
        try:
            msg = await self._bot.send_message(tg_id, text, reply_markup=markup)
        except TelegramForbiddenError as exc:  # бот заблокирован владельцем
            raise DeliveryError(type(exc).__name__, permanent=True) from exc
        except TelegramBadRequest as exc:  # чат не найден и т. п.
            raise DeliveryError(type(exc).__name__, permanent=True) from exc
        except (TelegramNetworkError, TelegramRetryAfter) as exc:
            raise DeliveryError(type(exc).__name__, permanent=False) from exc
        return msg.message_id

    async def send_reminder(self, tg_id: int, due: Due, text: str) -> int:
        return await self._send(tg_id, text, reminder_keyboard(due.reminder.id))

    async def send_proactive(self, tg_id: int, text: str) -> int:
        return await self._send(tg_id, text)
