"""The flipbook: plays a run of photos from one camera folder quickly, one after another, so photos taken a moment
apart look like a film (a landing, a robotic arm moving, the sky changing).

FlipbookPlayer reads the frames on background threads, a little ahead of the one shown, and only moves on when the
next frame is ready, so a slow disk makes it pause rather than skip. FlipbookBar is the row of controls under the
full-size view.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from photo_library import PhotoFile

FPS_MIN, FPS_MAX = 1, 30
KEEP_ALL = 150              # frames kept in memory for looping without reading them again; longer runs stream


class FlipbookPlayer(QObject):
    """frame(photo, pixmap): show this frame. status(text): "Frame 3 of 40", or what it's waiting for."""

    frame = Signal(object, QPixmap)
    status = Signal(str)
    finished = Signal()                # reached the last frame without Loop

    def __init__(self, load_size: Callable[[], int], parent=None):
        super().__init__(parent)
        from photo_page import FULL_SIZE, ImageLoader     # here: photo_page imports this module
        self._full_size_text = FULL_SIZE
        self.load_size = load_size     # the longest side frames are read at (the view's size)
        self._waiting_for: int | None = None              # a frame to show as soon as it's read
        self.loader = ImageLoader(threads=2, max_waiting=400)
        self.loader.ready.connect(self._ready)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.frames: list[PhotoFile] = []
        self.index = -1                # the frame shown
        self.pix: dict[str, QPixmap] = {}
        self.sizes: dict[str, str] = {}
        self.failed: set[str] = set()
        self.fps = 8.0
        self.loop = True
        self.same_size = True
        self.playing = False
        self._gen = 0
        self._size = 0

    # ---- control -----------------------------------------------------------------------------------------

    def load(self, frames: list[PhotoFile], start: int = 0) -> None:
        """New frames (stops playing). Shows the start frame as soon as it's read."""
        self.pause()
        self._gen += 1
        self.loader.clear()
        self.loader.clear_low()
        keep = {p.path for p in frames}
        self.pix = {k: v for k, v in self.pix.items() if k in keep and self._size == self.load_size()}
        self._size = self.load_size()
        self.frames = list(frames)
        self.index = -1
        if self.frames:
            self._want(min(max(start, 0), len(self.frames) - 1), show=True)

    def play(self) -> None:
        if not self.frames:
            return
        if not self.loop and self.index >= len(self.frames) - 1:
            self.index = -1                # at the end: play from the start
        self.playing = True
        self.timer.start(max(1, round(1000 / self.fps)))
        self._read_ahead()

    def pause(self) -> None:
        self.playing = False
        self.timer.stop()
        self._show_status()

    def set_fps(self, fps: float) -> None:
        self.fps = min(max(float(fps), FPS_MIN), FPS_MAX)
        if self.playing:
            self.timer.setInterval(max(1, round(1000 / self.fps)))

    def close(self) -> None:
        self.pause()
        self.loader.close()

    def shown(self) -> PhotoFile | None:
        return self.frames[self.index] if 0 <= self.index < len(self.frames) else None

    # ---- frames ------------------------------------------------------------------------------------------

    def _key(self, p: PhotoFile):
        return ("frame", self._gen, p.path)

    def _want(self, i: int, show: bool = False) -> None:
        p = self.frames[i]
        if p.path in self.pix:
            if show:
                self._show(i)
            return
        if show:
            self._waiting_for = i
        self.loader.request(self._key(p), p.path, self._size)
        if show:
            self.status.emit("Loading…")

    def _skipped(self, p: PhotoFile, ref: str | None) -> bool:
        """Known to be unplayable: unreadable, or (with Skip Odd Sizes) read before and another size. A frame's
        size is kept after its picture is let go, so a skipped frame is never read again just to skip it."""
        size = self.sizes.get(p.path)
        return p.path in self.failed or bool(ref and size and size != ref)

    def _upcoming(self) -> list[int]:
        """The next frames to show after the one shown: ones that play and ones not read yet. Frames known to be
        skipped don't count, so a long run of thumbnails can't use up the read-ahead."""
        n = len(self.frames)
        ahead = max(12, int(self.fps * 2))
        ref = self._reference_size() if self.same_size else None
        out = []
        for step in range(1, n + 1):
            i = self.index + step
            if i >= n:
                if not self.loop:
                    break
                i %= n
            if not self._skipped(self.frames[i], ref):
                out.append(i)
                if len(out) >= ahead:
                    break
        return out

    def _read_ahead(self) -> None:
        n = len(self.frames)
        upcoming = self._upcoming()
        for i in upcoming:
            p = self.frames[i]
            if p.path not in self.pix:
                self.loader.request(self._key(p), p.path, self._size, low=True)
        if n > KEEP_ALL:                  # a long run: keep only the frame shown, two before it and the next ones
            near = {self.frames[(self.index + d) % n].path for d in range(-2, 1)}
            near.update(self.frames[i].path for i in upcoming)
            for path in [k for k in self.pix if k not in near]:
                del self.pix[path]

    def _ready(self, key, img: QImage) -> None:
        if not (isinstance(key, tuple) and key[0] == "frame" and key[1] == self._gen):
            return
        path = key[2]
        if img.isNull():
            self.failed.add(path)
        else:
            self.pix[path] = QPixmap.fromImage(img)
            self.sizes[path] = img.text(self._full_size_text)
        w = self._waiting_for
        if w is not None and 0 <= w < len(self.frames) and self.frames[w].path == path:
            self._waiting_for = None
            if path in self.pix:
                self._show(w)
            elif not self.playing:
                self.status.emit("Can't show this file")

    def _reference_size(self) -> str | None:
        """The size the first readable frame has; with Same Size Only, other sizes are skipped."""
        for p in self.frames:
            if p.path in self.sizes:
                return self.sizes[p.path]
            if p.path not in self.failed:
                return None              # not read yet
        return None

    def _show(self, i: int) -> None:
        self.index = i
        p = self.frames[i]
        self.frame.emit(p, self.pix[p.path])
        self._show_status()

    def _show_status(self) -> None:
        if self.frames and self.index >= 0:
            self.status.emit(f"Frame {self.index + 1:,} of {len(self.frames):,}")

    def _tick(self) -> None:
        n = len(self.frames)
        if not n:
            return self.pause()
        ref = self._reference_size() if self.same_size else None
        i = self.index
        for _ in range(n):               # the next frame to show, skipping unreadable and other-size ones
            i += 1
            if i >= n:
                if not self.loop:
                    self.pause()
                    self.finished.emit()
                    return
                i = 0
            p = self.frames[i]
            if self._skipped(p, ref):    # before the check below: a skipped frame's picture isn't kept
                continue
            if p.path not in self.pix:   # not read yet: wait for it (a slow disk pauses, never skips)
                self._waiting_for = None
                self.loader.request(self._key(p), p.path, self._size)
                self.status.emit(f"Loading frame {i + 1:,} of {n:,}…")
                self._read_ahead()
                return
            self._show(i)
            self._read_ahead()
            return
        self.pause()                     # nothing playable


class FlipbookBar(QWidget):
    """The flipbook's controls under the full-size view. The page connects them to the player."""

    playing_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("flipbookBar")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(6)
        row1 = QHBoxLayout()
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setObjectName("primary")
        self.play_btn.setToolTip("Play as a flipbook (P): the photos you selected, or this photo's whole camera "
                                 "folder")
        row1.addWidget(self.play_btn)
        row1.addSpacing(10)
        row1.addWidget(QLabel("Speed"))
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(FPS_MIN, FPS_MAX)
        self.speed.setValue(8)
        self.speed.setFixedWidth(160)
        row1.addWidget(self.speed)
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("muted")
        self.speed_label.setMinimumWidth(self.speed_label.fontMetrics().horizontalAdvance("30 photos/s") + 6)
        row1.addWidget(self.speed_label)
        row1.addSpacing(10)
        self.loop_box = QCheckBox("Loop")
        self.loop_box.setChecked(True)
        self.loop_box.setToolTip("Start again from the first photo after the last")
        row1.addWidget(self.loop_box)
        self.same_box = QCheckBox("Skip Odd Sizes")
        self.same_box.setChecked(True)
        self.same_box.setToolTip("Skip photos that aren't the same size as the first one. NASA's folders often mix "
                                 "small thumbnails and cut-out parts in with the full photos, which makes the film "
                                 "jump; untick it to play every photo")
        row1.addWidget(self.same_box)
        row1.addStretch()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        row1.addWidget(self.status)
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        self.book_label = QLabel("")
        self.book_label.setObjectName("value")
        row2.addWidget(self.book_label)
        row2.addStretch()
        self.reverse_btn = QPushButton("Reverse")
        self.reverse_btn.setToolTip("Play the photos in the opposite order")
        row2.addWidget(self.reverse_btn)
        self.edit_btn = QPushButton("Edit Frames")
        self.edit_btn.setToolTip("Show this flipbook's frames as thumbnails: Delete removes, Ctrl+C and Ctrl+V "
                                 "copy and paste, right-click for more")
        row2.addWidget(self.edit_btn)
        self.save_btn = QPushButton("Save Flipbook…")
        self.save_btn.setToolTip("Keep these photos, in this order, with the speed and options, under Flipbooks")
        row2.addWidget(self.save_btn)
        self.save_new_btn = QPushButton("Save as New…")
        self.save_new_btn.setToolTip("Keep a copy of this flipbook under another name")
        row2.addWidget(self.save_new_btn)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.setToolTip("Delete this flipbook (its photos are kept)")
        row2.addWidget(self.delete_btn)
        lay.addLayout(row2)
        self.speed.valueChanged.connect(self._show_speed)
        self._show_speed(self.speed.value())
        self.set_book(None)

    def _show_speed(self, v: int) -> None:
        self.speed_label.setText(f"{v} photo/s" if v == 1 else f"{v} photos/s")

    def set_playing(self, playing: bool) -> None:
        self.play_btn.setText("❚❚  Pause" if playing else "▶  Play")
        self.playing_changed.emit(playing)

    def set_book(self, name: str | None) -> None:
        """name: the saved flipbook shown (its changes are saved as they're made); None: photos not saved yet."""
        saved = name is not None
        self.book_label.setText(f"Flipbook: {name}" if saved else "")
        self.book_label.setVisible(saved)
        self.save_btn.setVisible(not saved)
        self.edit_btn.setVisible(saved)
        self.save_new_btn.setVisible(saved)
        self.delete_btn.setVisible(saved)
