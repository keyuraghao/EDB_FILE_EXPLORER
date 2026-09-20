"""A small embedded terminal (PTY + VT100 emulation via ``pyte``) good enough to run interactive agent CLIs."""

from __future__ import annotations

import os
import shlex
import struct
import sys
import threading
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QKeyEvent, QPainter, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import QApplication, QWidget

try:
    import pyte
except ImportError:  # pragma: no cover
    pyte = None  # type: ignore[assignment]

_ANSI = {
    "black": "#1e1e1e",
    "red": "#e06c75",
    "green": "#98c379",
    "brown": "#d19a66",
    "yellow": "#e5c07b",
    "blue": "#61afef",
    "magenta": "#c678dd",
    "cyan": "#56b6c2",
    "white": "#d7dae0",
    "brightblack": "#5c6370",
    "brightred": "#ef7b85",
    "brightgreen": "#a9d38b",
    "brightyellow": "#f0d288",
    "brightblue": "#7fc0f5",
    "brightmagenta": "#d38fe6",
    "brightcyan": "#6ecbd6",
    "brightwhite": "#ffffff",
}
_KEYMAP = {
    Qt.Key.Key_Up: b"\x1b[A",
    Qt.Key.Key_Down: b"\x1b[B",
    Qt.Key.Key_Right: b"\x1b[C",
    Qt.Key.Key_Left: b"\x1b[D",
    Qt.Key.Key_Home: b"\x1b[H",
    Qt.Key.Key_End: b"\x1b[F",
    Qt.Key.Key_PageUp: b"\x1b[5~",
    Qt.Key.Key_PageDown: b"\x1b[6~",
    Qt.Key.Key_Insert: b"\x1b[2~",
    Qt.Key.Key_Delete: b"\x1b[3~",
    Qt.Key.Key_Escape: b"\x1b",
    Qt.Key.Key_Tab: b"\t",
    Qt.Key.Key_Backtab: b"\x1b[Z",
    Qt.Key.Key_Return: b"\r",
    Qt.Key.Key_Enter: b"\r",
    Qt.Key.Key_Backspace: b"\x7f",
    Qt.Key.Key_F1: b"\x1bOP",
    Qt.Key.Key_F2: b"\x1bOQ",
    Qt.Key.Key_F3: b"\x1bOR",
    Qt.Key.Key_F4: b"\x1bOS",
    Qt.Key.Key_F5: b"\x1b[15~",
    Qt.Key.Key_F6: b"\x1b[17~",
    Qt.Key.Key_F7: b"\x1b[18~",
    Qt.Key.Key_F8: b"\x1b[19~",
    Qt.Key.Key_F9: b"\x1b[20~",
    Qt.Key.Key_F10: b"\x1b[21~",
    Qt.Key.Key_F11: b"\x1b[23~",
    Qt.Key.Key_F12: b"\x1b[24~",
}


def terminal_available() -> tuple[bool, str]:
    if pyte is None:
        return False, "the 'pyte' package is not installed (pip install pyte)"
    if sys.platform == "win32":
        try:
            import winpty  # noqa: F401
        except ImportError:
            return False, "the 'pywinpty' package is not installed (pip install pywinpty)"
    return True, ""


class _Pty(QObject):
    """Cross-platform pseudo-terminal process wrapper. Emits ``data`` from a reader thread."""

    data = Signal(bytes)
    exited = Signal(int)

    def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str], rows: int, cols: int) -> None:
        super().__init__()
        self._proc: Any = None
        self._fd: int | None = None
        self._win: Any = None
        self._alive = True
        if sys.platform == "win32":
            import winpty

            self._win = winpty.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
        else:
            import pty
            import subprocess

            master, slave = pty.openpty()
            self._fd = master
            self._set_winsize(rows, cols)

            def _become_session_leader() -> None:  # runs in the child between fork and exec
                os.setsid()
                try:
                    import fcntl
                    import termios

                    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)  # make the pty the controlling terminal
                except OSError:
                    pass

            self._proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=env,
                preexec_fn=_become_session_leader,  # noqa: PLW1509 - spawned from the GUI thread only
                close_fds=True,
            )
            os.close(slave)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        code = 0
        try:
            if self._win is not None:
                while self._alive and self._win.isalive():
                    try:
                        chunk = self._win.read(4096)
                    except EOFError:
                        break
                    if chunk:
                        self.data.emit(chunk.encode("utf-8", "surrogatepass") if isinstance(chunk, str) else chunk)
                code = self._win.exitstatus or 0
            else:
                assert self._fd is not None
                while self._alive:
                    try:
                        chunk = os.read(self._fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.data.emit(chunk)
                if self._proc is not None:
                    code = self._proc.wait()
        finally:
            self.exited.emit(code)

    def write(self, data: bytes) -> None:
        try:
            if self._win is not None:
                self._win.write(data.decode("utf-8", "replace"))
            elif self._fd is not None:
                os.write(self._fd, data)
        except OSError:
            pass

    def _set_winsize(self, rows: int, cols: int) -> None:
        if self._fd is None:
            return
        import fcntl
        import termios

        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, rows: int, cols: int) -> None:
        try:
            if self._win is not None:
                self._win.setwinsize(rows, cols)
            else:
                self._set_winsize(rows, cols)
                if self._proc is not None and self._proc.poll() is None:
                    import signal

                    os.killpg(self._proc.pid, signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def terminate(self) -> None:
        self._alive = False
        try:
            if self._win is not None:
                self._win.terminate(force=True)
            elif self._proc is not None and self._proc.poll() is None:
                import signal

                os.killpg(self._proc.pid, signal.SIGHUP)
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    os.killpg(self._proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    @property
    def alive(self) -> bool:
        if self._win is not None:
            return bool(self._win.isalive())
        return self._proc is not None and self._proc.poll() is None


class TerminalWidget(QWidget):
    """Renders a ``pyte`` screen and forwards keystrokes to the child process."""

    exited = Signal(int)
    title_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None, font_size: int = 11) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        font = QFont("Monospace", font_size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        fm = QFontMetricsF(font)
        self._cw = fm.horizontalAdvance("M")
        self._ch = fm.height()
        self._ascent = fm.ascent()
        self.cols, self.rows = 100, 30
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=5000, ratio=0.3) if pyte else None
        self.stream = pyte.ByteStream(self.screen) if pyte else None
        self._pty: _Pty | None = None
        self._pending = bytearray()
        self._lock = threading.Lock()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        self._bg = QColor("#141618")
        self._fg = QColor("#d7dae0")
        self._exit_code: int | None = None
        self.argv: list[str] = []

    # -- process ------------------------------------------------------------ #
    def start(self, argv: list[str] | str, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        if isinstance(argv, str):
            argv = shlex.split(argv, posix=sys.platform != "win32")
        self.argv = list(argv)
        full_env = dict(os.environ)
        full_env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": full_env.get("LANG") or "C.UTF-8"})
        full_env.update(env or {})
        self._pty = _Pty(self.argv, cwd, full_env, self.rows, self.cols)
        self._pty.data.connect(self._on_data)
        self._pty.exited.connect(self._on_exit)
        self.title_changed.emit(" ".join(self.argv))

    def _on_data(self, chunk: bytes) -> None:
        with self._lock:
            self._pending += chunk
        if not self._timer.isActive():
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            data, self._pending = bytes(self._pending), bytearray()
        if data and self.stream is not None:
            try:
                self.stream.feed(data)
            except Exception:
                pass
            self.update()

    def _on_exit(self, code: int) -> None:
        self._exit_code = code
        self._flush()
        self.update()
        self.exited.emit(code)

    def send(self, data: bytes | str) -> None:
        if self._pty is not None and self._pty.alive:
            self._pty.write(data.encode("utf-8") if isinstance(data, str) else data)

    def terminate(self) -> None:
        if self._pty is not None:
            self._pty.terminate()

    @property
    def alive(self) -> bool:
        return self._pty is not None and self._pty.alive

    def screen_text(self) -> str:
        if self.screen is None:
            return ""
        return "\n".join(line.rstrip() for line in self.screen.display)

    # -- geometry ------------------------------------------------------------ #
    def resizeEvent(self, event: QResizeEvent) -> None:
        cols = max(20, int(self.width() / self._cw))
        rows = max(5, int(self.height() / self._ch))
        if (cols, rows) != (self.cols, self.rows) and self.screen is not None:
            self.cols, self.rows = cols, rows
            self.screen.resize(rows, cols)
            if self._pty is not None:
                self._pty.resize(rows, cols)
        super().resizeEvent(event)

    def sizeHint(self) -> Any:
        from PySide6.QtCore import QSize

        return QSize(int(self._cw * 100), int(self._ch * 30))

    # -- painting ------------------------------------------------------------ #
    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        if self.screen is None:
            p.setPen(self._fg)
            p.drawText(10, 20, "Terminal emulation unavailable: install 'pyte'.")
            return
        p.setFont(self.font())
        buffer = self.screen.buffer
        for y in range(self.rows):
            line = buffer[y]
            x = 0
            while x < self.cols:
                ch = line[x]
                run_start = x
                style = (ch.fg, ch.bg, ch.bold, ch.reverse, ch.underscore)
                text = []
                while (
                    x < self.cols
                    and (line[x].fg, line[x].bg, line[x].bold, line[x].reverse, line[x].underscore) == style
                ):
                    text.append(line[x].data or " ")
                    x += 1
                fg, bg = self._color(style[0], self._fg), self._color(style[1], self._bg)
                if style[3]:
                    fg, bg = bg, fg
                if style[2] and style[0] == "default":
                    fg = QColor("#ffffff")
                rx, ry = run_start * self._cw, y * self._ch
                if bg != self._bg:
                    p.fillRect(int(rx), int(ry), int(self._cw * (x - run_start)) + 1, int(self._ch) + 1, bg)
                p.setPen(fg)
                f = p.font()
                f.setBold(bool(style[2]))
                f.setUnderline(bool(style[4]))
                p.setFont(f)
                p.drawText(int(rx), int(ry + self._ascent), "".join(text))
        cur = self.screen.cursor
        if not cur.hidden and self.hasFocus() and self._exit_code is None:
            p.fillRect(
                int(cur.x * self._cw), int(cur.y * self._ch), int(self._cw), int(self._ch), QColor(215, 218, 224, 160)
            )
        if self._exit_code is not None:
            p.setPen(QColor("#8a9099"))
            p.drawText(8, int(self.height() - 6), f"[process exited with code {self._exit_code}]")

    def _color(self, name: str, default: QColor) -> QColor:
        if name == "default":
            return default
        if name in _ANSI:
            return QColor(_ANSI[name])
        if len(name) == 6:
            return QColor("#" + name)
        return default

    # -- input --------------------------------------------------------------- #
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._pty is None:
            return
        key, mods, text = event.key(), event.modifiers(), event.text()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        if ctrl and shift and key == Qt.Key.Key_V:
            self.send(QApplication.clipboard().text())
            return
        if ctrl and shift and key == Qt.Key.Key_C:
            QApplication.clipboard().setText(self.screen_text())
            return
        if key in _KEYMAP:
            seq = _KEYMAP[key]
            if alt:
                seq = b"\x1b" + seq
            self.send(seq)
            return
        if ctrl and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            self.send(bytes([key - Qt.Key.Key_A + 1]))
            return
        if ctrl and key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight, Qt.Key.Key_Backslash):
            self.send(bytes([{Qt.Key.Key_BracketLeft: 27, Qt.Key.Key_Backslash: 28, Qt.Key.Key_BracketRight: 29}[key]]))
            return
        if text:
            self.send(("\x1b" + text) if alt else text)
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event: Any) -> None:
        if self.screen is None:
            return
        if event.angleDelta().y() > 0:
            self.screen.prev_page()
        else:
            self.screen.next_page()
        self.update()

    def mousePressEvent(self, event: Any) -> None:
        self.setFocus()
        if event.button() == Qt.MouseButton.MiddleButton:
            self.send(
                QApplication.clipboard().text(QApplication.clipboard().Mode.Selection)
                or QApplication.clipboard().text()
            )
        super().mousePressEvent(event)

    def closeEvent(self, event: Any) -> None:
        self.terminate()
        super().closeEvent(event)
