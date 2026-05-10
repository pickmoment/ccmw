"""TerminalPanel — PTY 기반 인터랙티브 터미널 위젯."""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import signal
import struct
import termios
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

import pyte
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import MouseScrollDown, MouseScrollUp
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

# pyte가 처리하지 못하는 CSI 시퀀스 필터 (예: Kitty keyboard protocol ESC[=...u)
_UNSUPPORTED_CSI_RE = re.compile(rb"\x1b\[[=>][0-9;]*[A-Za-z]")

_HISTORY_SIZE = 500
_SCROLL_STEP = 3

# 키 → ANSI 바이트 매핑
_KEY_MAP: dict[str, bytes] = {
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~",
    "enter": b"\r",
    "tab": b"\t",
    "escape": b"\x1b",
    "backspace": b"\x7f",
    "ctrl+a": b"\x01",
    "ctrl+b": b"\x02",
    "ctrl+c": b"\x03",
    "ctrl+d": b"\x04",
    "ctrl+e": b"\x05",
    "ctrl+f": b"\x06",
    "ctrl+k": b"\x0b",
    "ctrl+l": b"\x0c",
    "ctrl+n": b"\x0e",
    "ctrl+p": b"\x10",
    "ctrl+r": b"\x12",
    "ctrl+u": b"\x15",
    "ctrl+w": b"\x17",
    "ctrl+z": b"\x1a",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
    "shift+tab": b"\x1b[Z",
}

# pyte 색상 이름 → Rich color() 인덱스
_PYTE_COLORS: dict[str, str] = {
    "black": "color(0)",
    "red": "color(1)",
    "green": "color(2)",
    "brown": "color(3)",
    "blue": "color(4)",
    "magenta": "color(5)",
    "cyan": "color(6)",
    "white": "color(7)",
    "brightblack": "color(8)",
    "brightred": "color(9)",
    "brightgreen": "color(10)",
    "brightbrown": "color(11)",
    "brightblue": "color(12)",
    "brightmagenta": "color(13)",
    "bfightmagenta": "color(13)",
    "brightcyan": "color(14)",
    "brightwhite": "color(15)",
}


def _to_rich_color(name: str) -> str | None:
    if name == "default":
        return None
    rich = _PYTE_COLORS.get(name)
    if rich:
        return rich
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return f"#{name}"
    return None


@lru_cache(maxsize=512)
def _cell_style(
    fg: str, bg: str, bold: bool, italics: bool, underscore: bool, reverse: bool
) -> str:
    _fg, _bg = (bg, fg) if reverse else (fg, bg)
    parts: list[str] = []
    c = _to_rich_color(_fg)
    if c:
        parts.append(c)
    c = _to_rich_color(_bg)
    if c:
        parts.append(f"on {c}")
    if bold:
        parts.append("bold")
    if italics:
        parts.append("italic")
    if underscore:
        parts.append("underline")
    return " ".join(parts)


def _render_row(row: dict, cols: int) -> Text:
    out = Text()
    run: list[str] = []
    run_style = ""
    prev_key: tuple | None = None

    for x in range(cols):
        cell = row[x]
        if cell.data == "":
            continue
        key = (cell.fg, cell.bg, cell.bold, cell.italics, cell.underscore, cell.reverse)
        if key == prev_key:
            run.append(cell.data)
        else:
            if run:
                out.append("".join(run), style=run_style)
            run = [cell.data]
            run_style = _cell_style(*key)
            prev_key = key

    if run:
        out.append("".join(run), style=run_style)
    return out


# ---------------------------------------------------------------------------
# Scrollbar widget
# ---------------------------------------------------------------------------

class TerminalScrollBar(Widget):
    """터미널 히스토리 위치를 표시하는 슬림 스크롤바."""

    DEFAULT_CSS = """
    TerminalScrollBar {
        width: 1;
        height: 1fr;
        background: $surface-lighten-1;
        color: $accent;
    }
    """

    class JumpTo(Message):
        """스크롤바 클릭 시 특정 오프셋으로 이동 요청."""
        def __init__(self, offset: int) -> None:
            super().__init__()
            self.offset = offset

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._max_history: int = 0
        self._visible: int = 24
        self._offset: int = 0   # 0 = 라이브(하단), N = N줄 위

    def update_position(self, max_history: int, visible: int, offset: int) -> None:
        self._max_history = max_history
        self._visible = visible
        self._offset = offset
        self.refresh()

    def render(self) -> Text:
        height = self.size.height
        if height <= 0:
            return Text()

        if self._max_history == 0:
            # 히스토리 없음 → 썸이 전체를 채움 (하단 고정)
            return Text("▋\n" * (height - 1) + "▋")

        total = self._max_history + self._visible
        thumb_h = max(1, round(height * self._visible / total))
        # 뷰포트 상단이 전체 컨텐츠에서 어느 위치인지 (위=0, 아래=max_history)
        viewport_top = self._max_history - self._offset
        thumb_top = min(
            round(height * viewport_top / total),
            height - thumb_h,
        )

        out = Text()
        for y in range(height):
            if y > 0:
                out.append("\n")
            if thumb_top <= y < thumb_top + thumb_h:
                out.append("▋")        # 썸: 위젯 전경색($accent)
            else:
                out.append(" ")        # 트랙: 위젯 배경색($surface-lighten-1)
        return out

    def on_click(self, event) -> None:
        height = self.size.height
        if height <= 0 or self._max_history == 0:
            return
        total = self._max_history + self._visible
        # 클릭 위치 → 전체 컨텐츠에서의 비율 → 뷰포트 상단 라인
        viewport_top = round(event.y * total / height)
        new_offset = max(0, min(self._max_history, self._max_history - viewport_top))
        self.post_message(self.JumpTo(new_offset))

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self.post_message(self.JumpTo(min(self._offset + _SCROLL_STEP, self._max_history)))
        event.stop()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self.post_message(self.JumpTo(max(self._offset - _SCROLL_STEP, 0)))
        event.stop()


# ---------------------------------------------------------------------------
# TerminalPanel
# ---------------------------------------------------------------------------

class TerminalPanel(Widget):
    """PTY 기반 인터랙티브 터미널 위젯 (스크롤바 포함)."""

    DEFAULT_CSS = """
    TerminalPanel {
        width: 1fr;
        height: 1fr;
        background: $surface-darken-1;
        layout: horizontal;
    }
    TerminalPanel #terminal-output {
        width: 1fr;
        height: 1fr;
        background: $surface-darken-1;
        color: $text;
        text-wrap: nowrap;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("shift+up", "scroll_up", "스크롤 위", show=False),
        Binding("shift+down", "scroll_down", "스크롤 아래", show=False),
        Binding("shift+pageup", "scroll_page_up", "페이지 위", show=False),
        Binding("shift+pagedown", "scroll_page_down", "페이지 아래", show=False),
    ]
    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._master_fd: int | None = None
        self._pid: int | None = None
        self._screen: pyte.HistoryScreen | None = None
        self._stream: pyte.ByteStream | None = None
        self._running: bool = False
        self._cols: int = 80
        self._rows: int = 24
        self._scroll_offset: int = 0  # 0 = 라이브 뷰, N = N줄 위

    def compose(self) -> ComposeResult:
        yield Static("터미널이 비어 있습니다.", id="terminal-output")
        yield TerminalScrollBar(id="terminal-scrollbar")

    def on_mount(self) -> None:
        cr = self.content_region
        self._cols = max((cr.width or 81) - 1, 40)  # 스크롤바 1열 제외
        self._rows = max(cr.height or 24, 10)
        self._init_pyte()

    def _init_pyte(self) -> None:
        self._screen = pyte.HistoryScreen(self._cols, self._rows, history=_HISTORY_SIZE)
        self._stream = pyte.ByteStream(self._screen)

    def start(self, cwd: Path | str, command: list[str] | None = None) -> None:
        if command is None:
            command = ["claude"]
        cwd = Path(cwd)

        self._stop_current()
        self._init_pyte()
        self._scroll_offset = 0

        try:
            self._pid, self._master_fd = pty.fork()
        except OSError as exc:
            self.query_one("#terminal-output", Static).update(f"PTY 생성 실패: {exc}")
            return

        if self._pid == 0:
            try:
                os.chdir(str(cwd))
            except OSError:
                pass
            os.execvp(command[0], command)
            os._exit(1)

        self._set_winsize(self._master_fd, self._rows, self._cols)
        os.set_blocking(self._master_fd, False)

        try:
            loop = asyncio.get_running_loop()
            loop.add_reader(self._master_fd, self._on_pty_readable)
        except RuntimeError:
            pass

        self._running = True
        self.query_one("#terminal-output", Static).update("")

    def _stop_current(self) -> None:
        if self._master_fd is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.remove_reader(self._master_fd)
            except (RuntimeError, ValueError):
                pass

        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                pass
            self._pid = None

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        self._running = False

    def _on_pty_readable(self) -> None:
        if self._master_fd is None or self._stream is None:
            return

        chunks = []
        try:
            while True:
                data = os.read(self._master_fd, 4096)
                if not data:
                    break
                chunks.append(data)
        except BlockingIOError:
            pass
        except OSError:
            self._running = False
            self._cleanup_fd()
            return

        if chunks:
            data = _UNSUPPORTED_CSI_RE.sub(b"", b"".join(chunks))
            if data:
                prev_hist = len(self._screen.history.top)
                self._stream.feed(data)
                if self._scroll_offset > 0:
                    added = len(self._screen.history.top) - prev_hist
                    self._scroll_offset = min(
                        self._scroll_offset + added,
                        len(self._screen.history.top),
                    )
            self._update_display()

    def _cleanup_fd(self) -> None:
        if self._master_fd is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.remove_reader(self._master_fd)
            except (RuntimeError, ValueError):
                pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def _update_display(self) -> None:
        if self._screen is None:
            return

        screen = self._screen
        buf = screen.buffer
        out = Text()

        offset = min(self._scroll_offset, len(screen.history.top))

        if offset > 0:
            history_lines = list(screen.history.top)
            hist_slice = history_lines[-offset:]
            curr_count = max(0, screen.lines - len(hist_slice))

            first = True
            for row in hist_slice:
                if not first:
                    out.append("\n")
                out.append_text(_render_row(row, screen.columns))
                first = False

            for y in range(curr_count):
                if not first:
                    out.append("\n")
                out.append_text(_render_row(buf[y], screen.columns))
                first = False
        else:
            cursor = screen.cursor
            show_cursor = self._running and self.has_focus
            cur_y = cursor.y
            cur_x = cursor.x

            for y in range(screen.lines):
                if y > 0:
                    out.append("\n")
                row = buf[y]
                run: list[str] = []
                run_style = ""
                prev_key: tuple | None = None

                for x in range(screen.columns):
                    cell = row[x]
                    if cell.data == "":
                        continue

                    at_cursor = show_cursor and y == cur_y and x == cur_x
                    if at_cursor:
                        if run:
                            out.append("".join(run), style=run_style)
                            run = []
                        if cell.fg != "default" or cell.bg != "default":
                            cur_style = _cell_style(
                                cell.fg, cell.bg, cell.bold, cell.italics, cell.underscore, True
                            )
                        else:
                            cur_style = "reverse"
                        out.append(cell.data, style=cur_style)
                        prev_key = None
                    else:
                        key = (cell.fg, cell.bg, cell.bold, cell.italics, cell.underscore, cell.reverse)
                        if key == prev_key:
                            run.append(cell.data)
                        else:
                            if run:
                                out.append("".join(run), style=run_style)
                            run = [cell.data]
                            run_style = _cell_style(*key)
                            prev_key = key

                if run:
                    out.append("".join(run), style=run_style)

        try:
            self.query_one("#terminal-output", Static).update(out)
        except Exception:
            pass

        # 스크롤바 갱신
        try:
            sb = self.query_one("#terminal-scrollbar", TerminalScrollBar)
            sb.update_position(
                len(screen.history.top),
                screen.lines,
                self._scroll_offset,
            )
        except Exception:
            pass

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def on_resize(self, event) -> None:
        cr = self.content_region
        self._cols = max(cr.width - 1, 40)  # 스크롤바 1열 제외
        self._rows = max(cr.height, 10)
        if self._screen is not None:
            self._screen.resize(self._rows, self._cols)
        if self._master_fd is not None:
            self._set_winsize(self._master_fd, self._rows, self._cols)

    def on_key(self, event) -> None:
        if not self._running or self._master_fd is None:
            return

        if event.key == "ctrl+v":
            path = self._save_clipboard_image()
            if path:
                self._write_to_pty(path.encode("utf-8"))
                self.app.notify(f"이미지: {path}", timeout=4)
                event.stop()
                event.prevent_default()
                return

        key_bytes = _KEY_MAP.get(event.key)
        if key_bytes is None and event.character:
            key_bytes = event.character.encode("utf-8", errors="replace")

        if key_bytes is not None:
            if self._scroll_offset != 0:
                self._scroll_offset = 0
                self._update_display()
            try:
                os.write(self._master_fd, key_bytes)
            except OSError:
                self._running = False
                self._cleanup_fd()
            event.stop()
            event.prevent_default()

    def on_paste(self, event) -> None:
        if not self._running or self._master_fd is None:
            return
        text = event.text
        if not text:
            path = self._save_clipboard_image()
            if path:
                self._write_to_pty(path.encode("utf-8"))
                self.app.notify(f"이미지: {path}", timeout=4)
            return
        if self._scroll_offset != 0:
            self._scroll_offset = 0
            self._update_display()
        try:
            os.write(self._master_fd, text.encode("utf-8", errors="replace"))
        except OSError:
            self._running = False
            self._cleanup_fd()
        event.stop()

    def _write_to_pty(self, data: bytes) -> None:
        if self._scroll_offset != 0:
            self._scroll_offset = 0
            self._update_display()
        try:
            os.write(self._master_fd, data)
        except OSError:
            self._running = False
            self._cleanup_fd()

    def _save_clipboard_image(self) -> str | None:
        """클립보드 이미지를 임시 PNG 파일로 저장하고 경로를 반환. 이미지 없으면 None."""
        import subprocess
        import tempfile

        tmp = tempfile.mktemp(suffix=".png", prefix="ccmw_paste_")

        script_png = f"""
try
    set imgData to (the clipboard as «class PNGf»)
    set fileHandle to open for access (POSIX file "{tmp}") with write permission
    set eof of fileHandle to 0
    write imgData to fileHandle
    close access fileHandle
    return "{tmp}"
on error
    return ""
end try
"""
        try:
            result = subprocess.run(
                ["osascript", "-"],
                input=script_png,
                capture_output=True, text=True, timeout=5,
            )
            path = result.stdout.strip()
            if path:
                return path
        except Exception:
            pass

        # TIFF fallback → sips로 PNG 변환
        tmp_tiff = tempfile.mktemp(suffix=".tiff", prefix="ccmw_paste_")
        script_tiff = f"""
try
    set imgData to (the clipboard as «class TIFF»)
    set fileHandle to open for access (POSIX file "{tmp_tiff}") with write permission
    set eof of fileHandle to 0
    write imgData to fileHandle
    close access fileHandle
    return "{tmp_tiff}"
on error
    return ""
end try
"""
        try:
            result = subprocess.run(
                ["osascript", "-"],
                input=script_tiff,
                capture_output=True, text=True, timeout=5,
            )
            tiff_path = result.stdout.strip()
            if tiff_path:
                conv = subprocess.run(
                    ["sips", "-s", "format", "png", tiff_path, "--out", tmp],
                    capture_output=True, timeout=10,
                )
                if conv.returncode == 0:
                    return tmp
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # 마우스 스크롤
    # ------------------------------------------------------------------

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self.action_scroll_up()
        event.stop()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self.action_scroll_down()
        event.stop()

    # ------------------------------------------------------------------
    # 스크롤바 클릭 → JumpTo 처리
    # ------------------------------------------------------------------

    def on_terminal_scroll_bar_jump_to(self, event: TerminalScrollBar.JumpTo) -> None:
        if self._screen is None:
            return
        max_offset = len(self._screen.history.top)
        self._scroll_offset = max(0, min(event.offset, max_offset))
        self._update_display()

    # ------------------------------------------------------------------
    # 스크롤 액션 (키보드)
    # ------------------------------------------------------------------

    def action_scroll_up(self) -> None:
        if self._screen is None:
            return
        max_offset = len(self._screen.history.top)
        self._scroll_offset = min(self._scroll_offset + _SCROLL_STEP, max_offset)
        self._update_display()

    def action_scroll_down(self) -> None:
        self._scroll_offset = max(self._scroll_offset - _SCROLL_STEP, 0)
        self._update_display()

    def action_scroll_page_up(self) -> None:
        if self._screen is None:
            return
        max_offset = len(self._screen.history.top)
        self._scroll_offset = min(self._scroll_offset + self._rows, max_offset)
        self._update_display()

    def action_scroll_page_down(self) -> None:
        self._scroll_offset = max(self._scroll_offset - self._rows, 0)
        self._update_display()

    def on_unmount(self) -> None:
        self._stop_current()

    def reset(self) -> None:
        self._stop_current()
        self._init_pyte()
        self._scroll_offset = 0
        try:
            self.query_one("#terminal-output", Static).update(
                "터미널이 비어 있습니다."
            )
        except Exception:
            pass
        try:
            sb = self.query_one("#terminal-scrollbar", TerminalScrollBar)
            sb.update_position(0, self._rows, 0)
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        return self._running and self._pid is not None
