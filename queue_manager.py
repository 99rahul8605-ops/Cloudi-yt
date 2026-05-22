"""
queue_manager.py — Global async task queue for download/upload jobs.

Design:
  • Max 2 tasks run concurrently (CONCURRENCY = 2).
  • All other submitted tasks are queued and assigned a position number.
  • Each queued user gets an immediate "🕐 Position N in queue" message,
    updated to "▶️ Starting your task…" when their turn arrives.
  • Position numbers are updated for everyone in the queue whenever
    a task completes and the queue shifts.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any

from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

CONCURRENCY = 2   # max simultaneous download+upload tasks

# ── Internal state ────────────────────────────────────────────────────────────
_semaphore: asyncio.Semaphore | None = None   # initialised in setup()
_queue: deque   = deque()                      # waiting jobs (TaskEntry)
_queue_lock     = asyncio.Lock()               # protect _queue mutations
_active_count   = 0                            # jobs currently running


@dataclass
class TaskEntry:
    coro_fn:   Callable[[], Awaitable[Any]]   # zero-arg async callable
    status_msg: Any                            # telegram Message to edit
    user_label: str = ""                       # e.g. title for log


def setup() -> None:
    """Call once from post_init to create the semaphore."""
    global _semaphore
    _semaphore = asyncio.Semaphore(CONCURRENCY)
    logger.info("Queue manager ready — concurrency=%d", CONCURRENCY)


# ── Public API ────────────────────────────────────────────────────────────────

async def enqueue(
    coro_fn:    Callable[[], Awaitable[Any]],
    status_msg: Any,
    user_label: str = "",
) -> None:
    """
    Submit a task.  If a slot is free it starts immediately; otherwise the
    caller's status_msg is updated with the queue position and the task waits.
    """
    if _semaphore is None:
        raise RuntimeError("queue_manager.setup() was not called")

    entry = TaskEntry(coro_fn=coro_fn, status_msg=status_msg, user_label=user_label)

    async with _queue_lock:
        # How many slots are actually free right now?
        free_slots = CONCURRENCY - _active_count
        if free_slots > 0:
            # Start immediately — don't queue
            asyncio.create_task(_run(entry))
            return

        # All slots busy — add to waiting queue and notify user
        _queue.append(entry)
        pos = len(_queue)          # 1-based position
        await _notify_position(entry, pos)
        logger.info("Task queued at position %d: %s", pos, user_label)


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _notify_position(entry: TaskEntry, pos: int) -> None:
    try:
        await entry.status_msg.edit_text(
            f"🕐 *Your task is queued*\n\n"
            f"📋 Position: *{pos}* in queue\n"
            f"⏳ Please wait — {CONCURRENCY} task(s) running in parallel.\n\n"
            f"_You'll be notified when your download starts._",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.debug("Could not notify queue position: %s", e)


async def _update_all_positions() -> None:
    """After a task completes, refresh position messages for all waiting tasks."""
    for i, entry in enumerate(_queue, start=1):
        try:
            await entry.status_msg.edit_text(
                f"🕐 *Your task is queued*\n\n"
                f"📋 Position: *{i}* in queue\n"
                f"⏳ Almost there — hang tight!\n\n"
                f"_You'll be notified when your download starts._",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.debug("Position update skipped for entry %d: %s", i, e)


async def _run(entry: TaskEntry) -> None:
    """Acquire semaphore, run the task, release and dispatch next queued job."""
    global _active_count

    async with _semaphore:
        async with _queue_lock:
            _active_count += 1

        try:
            await entry.status_msg.edit_text(
                "▶️ *Starting your download…*",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        logger.info("Task started: %s  (active=%d)", entry.user_label, _active_count)

        try:
            await entry.coro_fn()
        except Exception as e:
            logger.error("Task error [%s]: %s", entry.user_label, e, exc_info=True)
            try:
                await entry.status_msg.edit_text(
                    f"❌ Task failed: `{str(e)[:200]}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
        finally:
            async with _queue_lock:
                _active_count -= 1
                logger.info("Task finished: %s  (active=%d, queued=%d)",
                            entry.user_label, _active_count, len(_queue))

                if _queue:
                    next_entry = _queue.popleft()
                    # Update remaining queue positions
                    asyncio.create_task(_update_all_positions())
                    # Launch the next task
                    asyncio.create_task(_run(next_entry))
