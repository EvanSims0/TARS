"""A daily log file for every TARS process, with crashes written out in full.

Each command gets its own file (tars-voice-2026-10-10.log, tars-chat-…, desktop-…), so two
processes never rotate the same file. Logs can hold what was said, so they're kept only as long
as transcripts. Tracebacks never include variable values, which could hold a key or an email.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from loguru import logger


class _ToLoguru(logging.Handler):
    """Warnings from the standard library (asyncio's "Task exception was never retrieved")."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def log_to_dir(logs: Path, name: str, keep_days: int = 7) -> int:
    return logger.add(
        logs / f"{name}-{{time:YYYY-MM-DD}}.log",
        rotation="00:00", retention=f"{keep_days} days", level="INFO", encoding="utf-8",
        enqueue=True, backtrace=True, diagnose=False,
    )


def catch_crashes(where: str) -> None:
    """Uncaught errors, in the main thread or any other, go to the log with their traceback."""

    def main_hook(kind, error, tb) -> None:
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, error, tb)
            return
        logger.opt(exception=(kind, error, tb)).critical(f"{where} crashed")

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "a thread"
        logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).critical(
            f"{where}: {name} crashed")

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
    if not any(isinstance(h, _ToLoguru) for h in logging.root.handlers):
        logging.root.addHandler(_ToLoguru(level=logging.WARNING))


def start(data_dir: Path, name: str, keep_days: int = 7) -> int:
    sink = log_to_dir(data_dir / "logs", name, keep_days)
    catch_crashes(name)
    return sink
