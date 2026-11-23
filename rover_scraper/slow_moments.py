"""Notes the moments the window stops responding, and what it was doing, in slow_moments.log in the app data folder.

A timer on the window's thread ticks every 50 ms. A watcher thread checks that it keeps ticking; when it hasn't for
half a second, the watcher samples where the window's thread is (its Python call stack) until it ticks again, and
then writes one entry: when, for how long, what else was going on, and where the time went. Pauses longer than two
minutes are left out (the computer was asleep). The file is kept under 256 KB, oldest entries first to go.
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QTimer

THRESHOLD = 0.5               # seconds without a tick before it counts as a slow moment
SAMPLE = 0.1                  # seconds between samples of where the window's thread is
LONGEST = 120.0               # longer pauses are the computer sleeping, not the app
MAX_BYTES = 256 * 1024


class SlowMoments(QObject):
    def __init__(self, path: str, context: Callable[[], str] | None = None, parent=None):
        super().__init__(parent)
        self.path = path
        self.context = context            # what else was going on; called from the watcher thread
        self.beat = time.monotonic()
        self.main_id = threading.get_ident()
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._stop = False
        threading.Thread(target=self._watch, daemon=True, name="slow-moments").start()

    def stop(self) -> None:
        self._stop = True
        self.timer.stop()

    def _tick(self) -> None:
        self.beat = time.monotonic()

    def _where(self) -> str:
        frame = sys._current_frames().get(self.main_id)
        if frame is None:
            return "?"
        frames = traceback.extract_stack(frame)
        ours = [f for f in frames if os.path.basename(f.filename) not in ("threading.py",)]
        last = ours[-5:] if ours else frames[-5:]
        text = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(last))
        if len(ours) <= 3 and any(f.name == "main" for f in ours):
            text += "  (inside Qt: drawing, laying out, or a window being moved or resized)"
        return text

    def _watch(self) -> None:
        while not self._stop:
            time.sleep(SAMPLE)
            start = self.beat
            if time.monotonic() - start < THRESHOLD:
                continue
            samples: collections.Counter = collections.Counter()
            context = ""
            if self.context is not None:
                try:
                    context = self.context()
                except Exception as e:  # noqa: BLE001 - never let the watcher fail
                    context = f"(couldn't tell: {e})"
            while not self._stop and self.beat == start and time.monotonic() - start < LONGEST:
                samples[self._where()] += 1
                time.sleep(SAMPLE)
            if self.beat == start:            # still stuck after two minutes, or stopping: not this kind
                continue
            waited = self.beat - start
            if waited >= LONGEST:
                continue
            self._write(start, waited, context, samples)

    def _write(self, start: float, waited: float, context: str, samples: collections.Counter) -> None:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - (time.monotonic() - start)))
        total = sum(samples.values()) or 1
        lines = [f"{when}  the window waited {waited:.1f} s"]
        if context:
            lines.append(f"  {context}")
        lines.append("  where the time went:")
        for where, n in samples.most_common(4):
            lines.append(f"    {100 * n // total:3d}%  {where}")
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) > MAX_BYTES:
                with open(self.path, encoding="utf-8", errors="replace") as f:
                    old = f.read()
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(old[len(old) // 2:].split("\n\n", 1)[-1])     # the newer half, from a whole entry
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n\n")
        except OSError:
            pass
