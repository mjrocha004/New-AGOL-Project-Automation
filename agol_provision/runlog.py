"""Append-only record of what a run did, in order, with how long each step took.

Every diagnosis this project has needed came down to two questions: how long did
that take, and which item was it on. A view whose layers never arrived, an index
that failed once and worked unchanged on the next run, a process that sat for
ninety minutes after it had finished its work -- each was a timing question,
answered by reading a terminal scrollback out loud.

The log is written next to run state, as ``state/<slug>.log``, and appended to
across runs so a resume sits directly under the run it resumes. It records
logical operations rather than HTTP calls: one line per copy, per view, per index
pass, which is the grain the failures actually occurred at.

Two rules. **A log that breaks the run it is recording is worse than no log**, so
every write is swallowed. And ``--dry-run`` promises to write nothing at all, so a
``None`` path disables the whole thing rather than making the caller check.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Note:
    """A step's result, set once the step knows it."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail


class RunLog:
    """Writes to ``state/<slug>.log``. A ``None`` path makes every call a no-op."""

    def __init__(
        self,
        path: Path | None,
        *,
        clock: Any = time.monotonic,
        now: Any = _utc_now,
    ) -> None:
        self.path = path
        self._clock = clock
        self._now = now

    # ---------- writing ----------

    def _write(self, elapsed_ms: int | None, label: str, detail: str) -> None:
        if self.path is None:
            return
        stamp = f"{elapsed_ms}ms" if elapsed_ms is not None else ""
        line = f"{self._now()}  {stamp:>10}  {label}"
        if detail:
            line += f"  {detail}"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass  # never let the record break what it is recording

    def event(self, label: str, detail: str = "") -> None:
        """Something that happened at an instant, with no duration."""
        self._write(None, label, detail)

    @contextmanager
    def step(self, label: str, detail: str = "") -> Iterator[Note]:
        """Time a logical operation, recording it whether it succeeds or not.

        Yields a `Note` so a caller can name the outcome after the work -- how
        many indexes applied, how many layers a view got -- which is usually the
        part worth reading back.
        """
        note = Note(detail)
        started = self._clock()
        try:
            yield note
        except BaseException as exc:
            # BaseException, not Exception: `_fail` raises SystemExit, and a run
            # that stopped is exactly what the log needs to show.
            elapsed = int((self._clock() - started) * 1000)
            reason = " ".join(str(exc).split())
            self._write(
                elapsed,
                f"FAILED {label}",
                f"{note.detail}  {type(exc).__name__}: {reason}".strip(),
            )
            raise
        self._write(int((self._clock() - started) * 1000), label, note.detail)
