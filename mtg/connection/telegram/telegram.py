# -*- coding: utf-8 -*-
"""Telegram connection module."""

import asyncio
import logging
import queue
from types import SimpleNamespace
from typing import Any, Optional

import requests
from telegram import ReactionTypeEmoji, Update
from telegram.error import NetworkError, TelegramError
from telegram.ext import Application


class TelegramConnection:
    """Telegram connection wrapper built on python-telegram-bot Application."""

    def __init__(self, token: str, logger: logging.Logger):
        self.logger = logger
        self.token = token
        self.msg_queue: Optional[asyncio.Queue[tuple[tuple[Any, ...], dict[str, Any]]]] = None
        self.q: queue.Queue[tuple[tuple[Any, ...], dict[str, Any]]] = queue.Queue()
        self.queue_task: Optional[asyncio.Task[Any]] = None
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.application = Application.builder().token(token).job_queue(None).build()

    @property
    def bot(self):
        """Return the underlying Telegram bot instance."""

        return self.application.bot

    def send_message_sync(self, *args: Any, **kwargs: Any) -> None:
        """Send a Telegram message from a non-async context."""

        return self.send_message(*args, **kwargs)

    def send_message(self, *args: Any, **kwargs: Any) -> None:
        """Send a Telegram message from a synchronous context."""

        payload = dict(kwargs)
        if args:
            if len(args) > 0:
                payload.setdefault('chat_id', args[0])
            if len(args) > 1:
                payload.setdefault('text', args[1])
        if 'chat_id' not in payload or 'text' not in payload:
            self.logger.error("Telegram message requires chat_id and text, got %s", payload)
            return None
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            if not data.get('ok'):
                self.logger.error("Telegram API sendMessage failed: %s", data)
                return None
            result = data.get('result', {})
            return SimpleNamespace(
                message_id=result.get('message_id'),
                raw=result,
            )
        except (requests.RequestException, ValueError) as exc:
            self.logger.error("Failed to send Telegram message: %s", repr(exc))
            return None

    async def _send_reaction_async(
        self,
        chat_id: int,
        message_id: int,
        emoji: str,
        is_big: bool = False,
    ):
        """Attempt to set a Telegram reaction, fallback to a textual reply if unavailable."""

        try:
            await self.application.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
                is_big=is_big,
            )
            return True, None
        except (NetworkError, TelegramError, TypeError, ValueError) as exc:
            self.logger.warning("Falling back to textual reaction: %s", repr(exc))

        fallback = await self.application.bot.send_message(
            chat_id=chat_id,
            text=emoji,
            reply_to_message_id=message_id,
        )
        return False, fallback

    def send_reaction(
        self,
        chat_id: int,
        message_id: int,
        emoji: str,
        is_big: bool = False,
    ):
        """Set a Telegram reaction from a synchronous context."""

        if self.loop is None or self.loop.is_closed():
            self.logger.warning("Application loop not initialized yet, reaction will be dropped")
            return False, None
        future = asyncio.run_coroutine_threadsafe(
            self._send_reaction_async(
                chat_id=chat_id,
                message_id=message_id,
                emoji=emoji,
                is_big=is_big,
            ),
            self.loop,
        )
        try:
            return future.result(timeout=30)
        except Exception as exc:  # pylint:disable=broad-except
            self.logger.error("Failed to send Telegram reaction: %s", repr(exc))
            return False, None

    async def _process_message_queue(self) -> None:
        """Process queued Telegram messages."""

        if self.msg_queue is None:
            self.logger.error("Message queue not initialized")
            return

        while self.running:
            try:
                args, kwargs = self.q.get(timeout=1.0)
            except queue.Empty:
                try:
                    args, kwargs = await asyncio.wait_for(self.msg_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

            try:
                await self.application.bot.send_message(*args, **kwargs)
            except (NetworkError, TelegramError, TypeError, ValueError, RuntimeError) as exc:
                self.logger.error("Failed to send Telegram message: %s", repr(exc))

    async def start_queue_processor(self) -> None:
        """Start the async queue processor."""

        if self.running:
            return
        if self.msg_queue is None:
            self.msg_queue = asyncio.Queue()
        self.running = True
        self.queue_task = asyncio.create_task(self._process_message_queue())
        self.logger.info("Message queue processor started")

    async def stop_queue_processor(self) -> None:
        """Stop the async queue processor."""

        self.running = False
        if self.queue_task is not None:
            self.queue_task.cancel()
            try:
                await self.queue_task
            except asyncio.CancelledError:
                pass
            self.queue_task = None
            self.logger.info("Message queue processor stopped")

    def stop_queue_processor_sync(self) -> None:
        """Stop the async queue processor from a synchronous context."""

        self.running = False
        if self.loop is None or self.loop.is_closed():
            self.queue_task = None
            return
        if self.queue_task is not None:
            future = asyncio.run_coroutine_threadsafe(self.stop_queue_processor(), self.loop)
            try:
                future.result(timeout=5)
            except Exception:  # pylint:disable=broad-except
                self.logger.debug("Queue processor shutdown timed out", exc_info=True)
                self.queue_task = None

    def poll(self) -> None:
        """Run Telegram polling."""

        self.logger.info("Polling Telegram...")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.start_queue_processor())
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

    def shutdown(self) -> None:
        """Stop the Telegram bot."""

        self.stop_queue_processor_sync()
        self.application.stop_running()
