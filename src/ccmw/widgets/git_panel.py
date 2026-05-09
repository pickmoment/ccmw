"""GitPanel — VS Code 스타일 git 변경사항 패널."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.widgets import Button, Input, Label, ListItem, ListView
from textual import work


_STATUS_COLOR: dict[str, str] = {
    "M": "yellow",
    "A": "green",
    "D": "red",
    "R": "cyan",
    "C": "cyan",
    "U": "magenta",
    "?": "bright_black",
}


@dataclass
class _Change:
    xy: str    # raw XY from git status --porcelain
    path: str  # file path relative to repo root
    staged: bool  # index (X) column has a change


def _run(args: list[str], cwd: Path, timeout: int = 5) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _get_git_root(cwd: Path) -> Path | None:
    try:
        r = _run(["git", "rev-parse", "--show-toplevel"], cwd)
        return Path(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:
        return None


def _get_status(cwd: Path) -> list[_Change]:
    try:
        r = _run(["git", "status", "--porcelain"], cwd)
        changes: list[_Change] = []
        for line in r.stdout.splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[-1]
            staged = xy[0] not in (" ", "?")
            changes.append(_Change(xy, path, staged))
        return changes
    except Exception:
        return []


class GitPanel(Container):
    """Git 변경사항 보기, 스테이징, 커밋, 동기화 패널."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "refresh", "새로고침", show=True, priority=True),
        Binding("space", "toggle_stage", "스테이지 토글", show=True),
        Binding("a", "stage_all", "모두 스테이지", show=True),
        Binding("escape", "close", "닫기", show=False, priority=True),
    ]

    class CloseRequested(Message):
        pass

    def __init__(self, cwd: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cwd = cwd
        self._root: Path | None = None
        self._changes: list[_Change] = []

    def compose(self) -> ComposeResult:
        yield Label("git", id="git-panel-title")
        yield ListView(id="git-file-list")
        yield Input(placeholder="커밋 메시지...", id="git-commit-msg")
        with Horizontal(id="git-buttons"):
            yield Button("모두 스테이지 (a)", id="btn-stage-all")
            yield Button("커밋", id="btn-commit", variant="primary")
            yield Button("동기화 ↑↓", id="btn-sync", variant="warning")

    def on_mount(self) -> None:
        self._root = _get_git_root(self._cwd)
        self.action_refresh()

    def update_cwd(self, cwd: Path) -> None:
        self._cwd = cwd
        self._root = _get_git_root(cwd)
        self.action_refresh()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _work_dir(self) -> Path:
        return self._root or self._cwd

    def _rebuild_list(self) -> None:
        lv: ListView = self.query_one("#git-file-list", ListView)
        lv.clear()
        if not self._changes:
            lv.append(ListItem(Label("(변경사항 없음)")))
            return
        for c in self._changes:
            chk = "☑" if c.staged else "☐"
            code = c.xy[0] if c.staged else c.xy[1]
            col = _STATUS_COLOR.get(code, "")
            if col:
                text = f"{chk} [{col}]{code}[/{col}]  {c.path}"
            else:
                text = f"{chk} {code}  {c.path}"
            lv.append(ListItem(Label(text)))

    def _update_title(self) -> None:
        staged = sum(1 for c in self._changes if c.staged)
        total = len(self._changes)
        title = self.query_one("#git-panel-title", Label)
        if self._root is None:
            title.update("git  (저장소 없음)")
        else:
            title.update(f"git  변경 {total}  스테이징됨 {staged}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._changes = _get_status(self._work_dir())
        self._rebuild_list()
        self._update_title()

    def action_toggle_stage(self) -> None:
        lv: ListView = self.query_one("#git-file-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._changes):
            return
        change = self._changes[idx]
        cwd = self._work_dir()
        try:
            if change.staged:
                _run(["git", "restore", "--staged", change.path], cwd)
            else:
                _run(["git", "add", change.path], cwd)
            self.action_refresh()
        except Exception as exc:
            self.app.notify(str(exc), severity="error")

    def action_stage_all(self) -> None:
        try:
            _run(["git", "add", "-A"], self._work_dir())
            self.action_refresh()
        except Exception as exc:
            self.app.notify(str(exc), severity="error")

    def action_close(self) -> None:
        self.post_message(self.CloseRequested())

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def _do_commit(self) -> None:
        msg_input = self.query_one("#git-commit-msg", Input)
        msg = msg_input.value.strip()
        if not msg:
            self.app.notify("커밋 메시지를 입력하세요", severity="warning")
            msg_input.focus()
            return
        try:
            result = _run(["git", "commit", "-m", msg], self._work_dir(), timeout=30)
            if result.returncode == 0:
                msg_input.value = ""
                self.app.notify("커밋 완료", timeout=2)
                self.action_refresh()
            else:
                err = result.stderr.strip() or result.stdout.strip()
                self.app.notify(err, severity="error")
        except Exception as exc:
            self.app.notify(str(exc), severity="error")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "git-commit-msg":
            self._do_commit()

    # ------------------------------------------------------------------
    # Sync (push) — background thread
    # ------------------------------------------------------------------

    @work(thread=True)
    def _do_sync(self) -> None:
        cwd = self._work_dir()
        self.app.call_from_thread(self.app.notify, "동기화 중...", timeout=2)
        try:
            result = subprocess.run(
                ["git", "push"], cwd=str(cwd),
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(
                self.app.notify, "동기화 시간 초과", severity="error"
            )
            return
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return

        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, "동기화 완료", timeout=3)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        self.call_from_thread(self.action_refresh)

    # ------------------------------------------------------------------
    # Button handler
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-stage-all":
            self.action_stage_all()
        elif event.button.id == "btn-commit":
            self._do_commit()
        elif event.button.id == "btn-sync":
            self._do_sync()
