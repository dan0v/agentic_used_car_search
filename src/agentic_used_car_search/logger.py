"""Leveled stderr logging for the MCP server.

Everything writes to stderr, never stdout: stdout is the MCP transport and
anything written there corrupts the protocol stream. That is the whole reason
this module exists rather than callers reaching for print().

Levels, least to most verbose:

  silent  nothing at all
  error   failures only
  info    the default - one line per tool call and per scraper outcome
  debug   request arguments, built search URLs, timings, retry attempts,
          progress-token resolution, and stack traces on errors
  trace   debug plus response payload previews

Selecting a level, highest precedence first:

  CAR_DEALS_LOG_LEVEL=debug     env var wins - MCP clients set `env` in the
                                server config, so this is the knob that can
                                be changed without editing the launch args
  --trace                       argv flag
  --verbose / -v                argv flag, equivalent to debug
  (default)                     info
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import TracebackType

LEVELS: dict[str, int] = {
    'silent': 0,
    'error': 1,
    'info': 2,
    'debug': 3,
    'trace': 4,
}

_VALID_LEVELS = set(LEVELS)


def resolve_level(env: dict[str, str] | None = None, argv: list[str] | None = None) -> str:
    env = env if env is not None else dict(os.environ)
    argv = argv if argv is not None else sys.argv
    from_env = (env.get('CAR_DEALS_LOG_LEVEL') or '').strip().lower()
    if from_env:
        if from_env in _VALID_LEVELS:
            return from_env
        # An unrecognised value is a typo in someone's client config, and
        # silently falling back to info is how that typo survives for months.
        sys.stderr.write(
            f'[MCP] Unknown CAR_DEALS_LOG_LEVEL {from_env!r}, expected one of: '
            f"{', '.join(LEVELS)}. Using 'info'.\n"
        )
        return 'info'
    if '--trace' in argv:
        return 'trace'
    if '--verbose' in argv or '-v' in argv:
        return 'debug'
    return 'info'


level = resolve_level()
_threshold = LEVELS[level]


def _enabled(name: str) -> bool:
    return LEVELS[name] <= _threshold


def _write(line: str) -> None:
    sys.stderr.write(f'{line}\n')
    sys.stderr.flush()


def _emit(name: str, message: str) -> None:
    if not _enabled(name):
        return
    # At info/error the prefix is a bare [MCP], byte-identical to what this
    # server printed before levels existed, so existing log-scraping keeps
    # working. The more verbose levels tag themselves so a noisy run is easy
    # to filter.
    prefix = '[MCP]' if name in ('info', 'error') else f'[MCP:{name}]'
    _write(f'{prefix} {message}')


def _format_error(err: BaseException | Any) -> str:
    """Render an error for humans: the message chain first, then stacks.

    The scrapers wrap failures (raise X(...) from err), so the interesting
    frames live on __cause__, not on the outermost error. Without walking the
    chain the log says a navigation timed out but never which navigation.
    """
    if not isinstance(err, BaseException):
        return str(err)

    chain: list[BaseException] = []
    current: BaseException | None = err
    depth = 0
    while isinstance(current, BaseException) and depth < 5:
        chain.append(current)
        current = current.__cause__
        depth += 1

    # Stacks are debug-only: at info an error stays a single readable line.
    if not _enabled('debug'):
        return str(err)

    parts = [''.join(traceback.format_exception(type(err), err, err.__traceback__)).rstrip()]
    for link in chain[1:]:
        parts.append(f'  caused by: {link!r}')
    return '\n'.join(parts)


def preview(value: Any, max_len: int = 400) -> str:
    """Shorten a value for logging. Tool results run to tens of thousands of
    characters (a detail page is capped at 30k), which is not something to
    paste into a log line whole.
    """
    text = value if isinstance(value, str) else _safe_stringify(value)
    if len(text) <= max_len:
        return text
    return f'{text[:max_len]}… ({len(text)} chars total)'


def _safe_stringify(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception as err:  # noqa: BLE001
        return f'[unserialisable: {err}]'


class _Logger:
    level = level
    threshold = _threshold
    enabled = staticmethod(_enabled)
    preview = staticmethod(preview)

    @staticmethod
    def info(message: str) -> None:
        _emit('info', message)

    @staticmethod
    def debug(message: str) -> None:
        _emit('debug', message)

    @staticmethod
    def trace(message: str) -> None:
        _emit('trace', message)

    @staticmethod
    def error(message: str, err: Any = None) -> None:
        if not _enabled('error'):
            return
        detail = '' if err is None else f': {_format_error(err)}'
        _write(f'[MCP] {message}{detail}')


logger = _Logger()


def install_global_handlers() -> None:
    """Catch failures that escape every handler - an unhandled exception in an
    asyncio task, a throw from a signal callback. Without this the process
    dies (or, worse, limps on) having written nothing to stderr, and the
    client sees a transport that stopped answering for no stated reason.
    """

    def _uncaught(
        exc_type: type[BaseException],
        exc: BaseException,
        _tb: TracebackType | None,
    ) -> None:
        logger.error('Uncaught exception', exc)
        # An uncaught exception leaves the process in an undefined state; the
        # MCP client will notice the transport close and can restart us.
        sys.exit(1)

    sys.excepthook = _uncaught


def _unhandled(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    logger.error('Unhandled asyncio exception', context.get('exception') or context)


def attach_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Set the asyncio exception handler on `loop`. Called from the
    `loop_factory` passed to `asyncio.run`, so the handler lands on the
    server's own loop; a `get_event_loop()` call at import time would only
    see a loop if one already existed (usually none) and is a
    DeprecationWarning on Python 3.12+.
    """
    loop.set_exception_handler(_unhandled)


__all__ = [
    'LEVELS',
    'install_global_handlers',
    'logger',
    'preview',
    'resolve_level',
]
