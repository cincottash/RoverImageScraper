"""Export Flipbook: saves a flipbook as an animated GIF or animated WebP file, the way it plays (its order, speed,
loop and Skip Odd Sizes), at a chosen size. The photos are only read; the file is written next to its final name
as <name>.part and renamed when it's complete, so a cancelled or failed export leaves nothing behind.
"""
from __future__ import annotations

import os
import threading
from typing import Callable

from PIL import Image
from PySide6.QtCore import QObject, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QImageReader, QPainter
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout)

from photo_library import PhotoFile

FORMATS = {"gif": ("GIF", ".gif"), "webp": ("Animated WebP", ".webp")}
SIZES = [(480, "480 px"), (720, "720 px"), (1080, "1080 px"), (0, "Original Size")]
# Frames are kept in memory until the file is written (the image library needs them all at once): above this,
# a smaller size is asked for instead of running the computer out of memory.
MEMORY_LIMIT = 1_500_000_000


class Cancelled(Exception):
    pass


def frame_sizes(paths: list[str], cancel: threading.Event | None = None) -> dict[str, tuple[int, int]]:
    """Each readable file's size, from its header (no picture is read)."""
    out = {}
    for path in dict.fromkeys(paths):
        if cancel is not None and cancel.is_set():
            raise Cancelled
        s = QImageReader(path).size()
        if s.isValid():
            out[path] = (s.width(), s.height())
    return out


def frames_to_export(frames: list[PhotoFile], sizes: dict[str, tuple[int, int]],
                     skip_odd: bool) -> tuple[list[PhotoFile], int]:
    """(the frames that play, how many are left out): unreadable ones, and with Skip Odd Sizes ones that aren't
    the size of the first readable frame (as the player does)."""
    ref = next((sizes[p.path] for p in frames if p.path in sizes), None)
    keep = [p for p in frames if p.path in sizes and (not skip_odd or sizes[p.path] == ref)]
    return keep, len(frames) - len(keep)


def canvas_size(first: tuple[int, int], longest: int) -> tuple[int, int]:
    """The file's picture size: the first frame's, shrunk so its longer side is at most longest (0: as it is)."""
    s = QSize(*first)
    if longest and max(s.width(), s.height()) > longest:
        s = s.scaled(longest, longest, Qt.AspectRatioMode.KeepAspectRatio)    # as read_image shrinks it
    return max(1, s.width()), max(1, s.height())


def bytes_needed(count: int, size: tuple[int, int], fmt: str) -> int:
    """Memory the frames take before the file is written: one byte a pixel for GIF, three for WebP."""
    return count * size[0] * size[1] * (1 if fmt == "gif" else 3)


def frame_ms(fps: float, fmt: str) -> int:
    """How long each frame shows. GIF counts in hundredths of a second, and most viewers slow frames shorter
    than 2/100 s, so a GIF frame is at least 20 ms."""
    ms = 1000.0 / max(fps, 0.1)
    if fmt == "gif":
        return max(20, int(round(ms / 10.0)) * 10)
    return max(1, int(round(ms)))


def _picture(path: str, canvas: tuple[int, int], gray_ok: bool) -> Image.Image:
    """One frame at the canvas size (another size is fitted inside, on black), as an image library picture:
    8-bit gray when the photo is gray and gray_ok, otherwise RGB."""
    from photo_page import read_image                  # here: photo_page imports this module
    w, h = canvas
    img = read_image(path, max(w, h))
    if img.isNull():
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor("black"))
    if (img.width(), img.height()) != (w, h):
        fitted = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        out = QImage(w, h, QImage.Format.Format_RGB32)
        out.fill(QColor("black"))
        p = QPainter(out)
        p.drawImage((w - fitted.width()) // 2, (h - fitted.height()) // 2, fitted)
        p.end()
        img = out
    if gray_ok and img.isGrayscale():
        img = img.convertToFormat(QImage.Format.Format_Grayscale8)
        return Image.frombuffer("L", (w, h), bytes(img.constBits()), "raw", "L", img.bytesPerLine(), 1).copy()
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    return Image.frombuffer("RGB", (w, h), bytes(img.constBits()), "raw", "RGB", img.bytesPerLine(), 1).copy()


def write_animation(paths: list[str], out_path: str, fmt: str, canvas: tuple[int, int], fps: float, loop: bool,
                    progress: Callable[[int, int], None] = lambda done, total: None,
                    cancel: threading.Event | None = None) -> int:
    """Write the frames (in this order) as an animated GIF or WebP file at out_path. progress(done, total) after
    each frame is read; total + 1 means the file is being written. Returns the file's size in bytes."""
    if not paths:
        raise ValueError("no frames to export")
    pictures = []
    for i, path in enumerate(paths):
        if cancel is not None and cancel.is_set():
            raise Cancelled
        pic = _picture(path, canvas, gray_ok=(fmt == "gif"))
        if fmt == "gif" and pic.mode == "RGB":         # 256 colors: the best palette for this frame
            pic = pic.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
        pictures.append(pic)
        progress(i + 1, len(paths))
    if cancel is not None and cancel.is_set():
        raise Cancelled
    progress(len(paths) + 1, len(paths))
    part = out_path + ".part"
    ms = frame_ms(fps, fmt)
    try:
        if fmt == "gif":
            extra = {"loop": 0} if loop else {}        # no loop value: viewers play it once
            pictures[0].save(part, "GIF", save_all=True, append_images=pictures[1:], duration=ms, disposal=1,
                             **extra)
        else:
            pictures[0].save(part, "WEBP", save_all=True, append_images=pictures[1:], duration=ms,
                             loop=0 if loop else 1, quality=82, method=4)
        os.replace(part, out_path)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return os.path.getsize(out_path)


def fmt_bytes(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} GB"


class _Signals(QObject):
    sized = Signal(object)                 # {path: (w, h)}
    progress = Signal(int, int)
    done = Signal(object, str)             # (file size or None, error text; "" when it worked or was cancelled)


class ExportDialog(QDialog):
    """The Export Flipbook window. ask_path(title, suggested path, filter) -> chosen path or None (replaced in
    tests, so no file dialog opens there)."""

    def __init__(self, parent, frames: list[PhotoFile], name: str, fps: float, loop: bool, skip_odd: bool,
                 settings, save_settings: Callable[[], None],
                 ask_path: Callable[[str, str, str], str | None]):
        super().__init__(parent)
        self.setWindowTitle("Export Flipbook")
        self.frames, self.name, self.fps, self.loop, self.skip_odd = frames, name, fps, loop, skip_odd
        self.settings, self.save_settings, self.ask_path = settings, save_settings, ask_path
        self.sizes: dict[str, tuple[int, int]] | None = None
        self.cancel = threading.Event()
        self.running = False
        self.saved_path: str | None = None
        self.sig = _Signals()
        self.sig.sized.connect(self._sized)
        self.sig.progress.connect(self._progress)
        self.sig.done.connect(self._done)

        lay = QVBoxLayout(self)
        title = QLabel(name)
        title.setObjectName("value")
        lay.addWidget(title)
        form = QFormLayout()
        self.format_box = QComboBox()
        for key, (label, _ext) in FORMATS.items():
            self.format_box.addItem(label, key)
        i = self.format_box.findData(getattr(settings, "export_format", "gif"))
        self.format_box.setCurrentIndex(max(0, i))
        self.format_box.setToolTip("GIF plays almost everywhere but has 256 colors per frame; animated WebP keeps "
                                   "full color in a smaller file and plays in web browsers")
        form.addRow("Format", self.format_box)
        self.size_box = QComboBox()
        for px, label in SIZES:
            self.size_box.addItem(label, px)
        i = self.size_box.findData(int(getattr(settings, "export_size", 720) or 0))
        self.size_box.setCurrentIndex(i if i >= 0 else 1)
        self.size_box.setToolTip("The longer side of the exported picture. Photos are never made larger")
        form.addRow("Size", self.size_box)
        speed = f"{fps:g} photo/s" if fps == 1 else f"{fps:g} photos/s"
        form.addRow("Speed", QLabel(f"{speed}   ·   Loop {'on' if loop else 'off'}"))
        self.frames_lbl = QLabel("Checking the frames' sizes…")
        form.addRow("Frames", self.frames_lbl)
        lay.addLayout(form)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.folder_btn = QPushButton("Show in Folder")
        self.folder_btn.setVisible(False)
        self.folder_btn.clicked.connect(self._show_in_folder)
        row.addWidget(self.folder_btn)
        row.addStretch()
        self.export_btn = QPushButton("Export…")
        self.export_btn.setObjectName("primary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export)
        row.addWidget(self.export_btn)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self._close_or_cancel)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.setMinimumWidth(440)
        self.format_box.currentIndexChanged.connect(self._update_note)
        self.size_box.currentIndexChanged.connect(self._update_note)
        self._update_note()
        paths = [p.path for p in frames]
        threading.Thread(target=self._read_sizes, args=(paths,), daemon=True, name="export-sizes").start()

    # ---- choices ---------------------------------------------------------------------------------------------

    def fmt(self) -> str:
        return self.format_box.currentData()

    def longest(self) -> int:
        return int(self.size_box.currentData())

    def chosen(self) -> tuple[list[PhotoFile], int]:
        return frames_to_export(self.frames, self.sizes or {}, self.skip_odd)

    def canvas(self) -> tuple[int, int] | None:
        keep, _ = self.chosen()
        return canvas_size(self.sizes[keep[0].path], self.longest()) if keep else None

    def _read_sizes(self, paths: list[str]) -> None:
        try:
            self.sig.sized.emit(frame_sizes(paths, self.cancel))
        except Cancelled:
            pass

    def _sized(self, sizes: dict) -> None:
        self.sizes = sizes
        keep, left_out = self.chosen()
        text = f"{len(keep):,}"
        if left_out:
            why = "of another size or unreadable" if self.skip_odd else "unreadable"
            text += f"   ({left_out:,} {why}, left out)"
        self.frames_lbl.setText(text)
        self._update_note()

    def _update_note(self) -> None:
        if self.running:
            return
        notes = []
        if self.fmt() == "gif":
            notes.append("GIF: 256 colors per frame.")
            if self.fps > 50:
                notes.append("Most viewers play GIF frames no faster than 50 a second.")
        problem = ""
        if self.sizes is not None:
            keep, _ = self.chosen()
            canvas = self.canvas()
            if not keep:
                problem = "None of the frames can be read."
            else:
                notes.insert(0, f"{canvas[0]:,} × {canvas[1]:,} pixels.")
                if bytes_needed(len(keep), canvas, self.fmt()) > MEMORY_LIMIT:
                    problem = (f"Too large to export at this size ({len(keep):,} frames): choose a smaller "
                               "size, or fewer frames with Edit Frames.")
        self.note.setText(problem or " ".join(notes))
        self.export_btn.setEnabled(self.sizes is not None and not problem)

    # ---- exporting -------------------------------------------------------------------------------------------

    def _export(self) -> None:
        fmt = self.fmt()
        label, ext = FORMATS[fmt]
        folder = getattr(self.settings, "export_folder", "") or os.path.expanduser("~")
        safe = "".join("-" if c in '<>:"/\\|?*' else c for c in self.name).strip(" .") or "Flipbook"
        path = self.ask_path("Export Flipbook", os.path.join(folder, safe + ext), f"{label} (*{ext})")
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        self.settings.export_format, self.settings.export_size = fmt, self.longest()
        self.settings.export_folder = os.path.dirname(path)
        self.save_settings()
        keep, _ = self.chosen()
        canvas = self.canvas()
        self.running = True
        self.cancel.clear()
        self.saved_path = None
        for w in (self.format_box, self.size_box, self.export_btn):
            w.setEnabled(False)
        self.folder_btn.setVisible(False)
        self.close_btn.setText("Cancel")
        self.bar.setVisible(True)
        self.bar.setRange(0, len(keep))
        self.bar.setValue(0)
        self.status.setText("Reading the frames…")
        threading.Thread(target=self._work, args=([p.path for p in keep], path, fmt, canvas),
                         daemon=True, name="export").start()

    def _work(self, paths, path, fmt, canvas) -> None:
        try:
            size = write_animation(paths, path, fmt, canvas, self.fps, self.loop,
                                   progress=self.sig.progress.emit, cancel=self.cancel)
            self.sig.done.emit((path, size), "")
        except Cancelled:
            self.sig.done.emit(None, "")
        except Exception as e:  # noqa: BLE001 - shown in the window
            self.sig.done.emit(None, f"Couldn't export: {e}")

    def _progress(self, done: int, total: int) -> None:
        if done > total:
            self.bar.setRange(0, 0)                    # writing the file: no steps to show
            self.status.setText("Writing the file…")
        else:
            self.bar.setValue(done)
            self.status.setText(f"Frame {done:,} of {total:,}")

    def _done(self, result, error: str) -> None:
        self.running = False
        for w in (self.format_box, self.size_box):
            w.setEnabled(True)
        self.bar.setVisible(False)
        self.close_btn.setText("Close")
        if result is not None:
            path, size = result
            self.saved_path = path
            self.status.setText(f"Saved {os.path.basename(path)} ({fmt_bytes(size)}).")
            self.folder_btn.setVisible(True)
        elif error:
            self.status.setText(error)
        else:
            self.status.setText("Cancelled. Nothing was saved.")
        self._update_note()

    def _show_in_folder(self) -> None:
        if self.saved_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.saved_path)))

    def _close_or_cancel(self) -> None:
        if self.running:
            self.cancel.set()
            self.status.setText("Cancelling…")
        else:
            self.reject()

    def reject(self) -> None:
        """Esc or the window's close button: cancel a running export first."""
        if self.running:
            self.cancel.set()
            self.status.setText("Cancelling…")
            return
        self.cancel.set()
        super().reject()
