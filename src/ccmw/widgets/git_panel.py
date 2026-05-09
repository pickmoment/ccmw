"""GitPanel — VS Code 스타일 git 변경사항 패널."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog
from textual import work

from ccmw.widgets.claude_review_modal import ClaudeReviewModal


class SyncedRichLog(RichLog):
    """peer와 스크롤 위치를 동기화하는 RichLog."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._peer: SyncedRichLog | None = None

    def link_peer(self, peer: SyncedRichLog) -> None:
        self._peer = peer
        peer._peer = self

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self._peer is not None and self._peer.scroll_y != new_value:
            self._peer.scroll_to(y=new_value, animate=False, immediate=True)

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        if self._peer is not None and self._peer.scroll_x != new_value:
            self._peer.scroll_to(x=new_value, animate=False, immediate=True)


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


def _to_unified(diff_text: str) -> list[Text]:
    lines: list[Text] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(Text(line, style="green"))
        elif line.startswith("-") and not line.startswith("---"):
            lines.append(Text(line, style="red"))
        elif line.startswith("@@"):
            lines.append(Text(line, style="cyan"))
        elif line.startswith(("diff ", "index ", "---", "+++")):
            lines.append(Text(line, style="bright_black"))
        else:
            lines.append(Text(line))
    return lines


def _to_split(diff_text: str) -> tuple[list[Text], list[Text]]:
    """unified diff를 (old, new) 컬럼 쌍으로 변환한다."""
    left: list[Text] = []
    right: list[Text] = []
    removals: list[str] = []
    additions: list[str] = []

    def flush() -> None:
        n = max(len(removals), len(additions)) if (removals or additions) else 0
        for i in range(n):
            l_line = removals[i] if i < len(removals) else ""
            r_line = additions[i] if i < len(additions) else ""
            left.append(Text(l_line, style="red" if l_line else ""))
            right.append(Text(r_line, style="green" if r_line else ""))
        removals.clear()
        additions.clear()

    for line in diff_text.splitlines():
        if line.startswith(("diff ", "index ", "---", "+++")):
            flush()
            left.append(Text(line, style="bright_black"))
            right.append(Text(line, style="bright_black"))
        elif line.startswith("@@"):
            flush()
            left.append(Text(line, style="cyan"))
            right.append(Text(line, style="cyan"))
        elif line.startswith("-"):
            if additions:
                flush()
            removals.append(line)
        elif line.startswith("+"):
            additions.append(line)
        else:
            flush()
            left.append(Text(line))
            right.append(Text(line))

    flush()
    return left, right


class GitPanel(Container):
    """Git 변경사항 보기, 스테이징, 커밋, 동기화 패널."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "refresh", "새로고침", show=True, priority=True),
        Binding("space", "toggle_stage", "스테이지 토글", show=True),
        Binding("a", "stage_all", "모두 스테이지", show=True),
        Binding("d", "toggle_diff_mode", "Diff 모드", show=True),
        Binding("v", "review", "AI 리뷰", show=True),
        Binding("escape", "close", "닫기", show=False, priority=True),
    ]

    class CloseRequested(Message):
        pass

    def __init__(self, cwd: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cwd = cwd
        self._root: Path | None = None
        self._changes: list[_Change] = []
        self._diff_mode: str = "unified"  # "unified" | "split"
        self._current_diff_text: str = ""
        self._current_diff_path: str = ""

    def compose(self) -> ComposeResult:
        yield Label("git", id="git-panel-title")
        with Horizontal(id="git-panel-body"):
            with Vertical(id="git-panel-left"):
                yield ListView(id="git-file-list")
                yield Input(placeholder="커밋 메시지...", id="git-commit-msg")
                with Horizontal(id="git-buttons"):
                    yield Button("모두 스테이지 (a)", id="btn-stage-all")
                    yield Button("커밋", id="btn-commit", variant="primary")
                    yield Button("동기화 ↑↓", id="btn-sync", variant="warning")
                yield Button("AI 리뷰 (v)", id="btn-review", variant="success")
            with Vertical(id="git-diff-pane"):
                yield Label("", id="git-diff-title")
                yield RichLog(id="git-diff-unified", markup=False, highlight=False, wrap=False)
                with Horizontal(id="git-diff-split", classes="hidden"):
                    yield SyncedRichLog(id="git-diff-old", markup=False, highlight=False, wrap=False)
                    yield SyncedRichLog(id="git-diff-new", markup=False, highlight=False, wrap=False)

    def on_mount(self) -> None:
        self._root = _get_git_root(self._cwd)
        self.action_refresh()
        old_log = self.query_one("#git-diff-old", SyncedRichLog)
        new_log = self.query_one("#git-diff-new", SyncedRichLog)
        old_log.link_peer(new_log)

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

    def _diff_title_text(self, path: str) -> str:
        mode = "split" if self._diff_mode == "split" else "unified"
        return f"{path}  [{mode}]"

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

    def action_toggle_diff_mode(self) -> None:
        if self._diff_mode == "unified":
            self._diff_mode = "split"
            self.query_one("#git-diff-unified").add_class("hidden")
            self.query_one("#git-diff-split").remove_class("hidden")
        else:
            self._diff_mode = "unified"
            self.query_one("#git-diff-split").add_class("hidden")
            self.query_one("#git-diff-unified").remove_class("hidden")
        # Update title to reflect new mode
        lv = self.query_one("#git-file-list", ListView)
        idx = lv.index
        if idx is not None and idx < len(self._changes):
            self.query_one("#git-diff-title", Label).update(
                self._diff_title_text(self._changes[idx].path)
            )

    def action_review(self) -> None:
        if not self._changes:
            self.app.notify("리뷰할 변경 파일이 없습니다", severity="warning")
            return
        combined = self._collect_all_diffs()
        if not combined:
            self.app.notify("리뷰할 diff가 없습니다 (새 파일만 있거나 변경사항 없음)", severity="warning")
            return
        file_count = len(self._changes)
        title = f"{file_count}개 파일 변경사항 전체"
        self.app.push_screen(
            ClaudeReviewModal(
                diff_text=combined,
                title=title,
                cwd=self._work_dir(),
            )
        )

    def _collect_all_diffs(self) -> str:
        """변경된 모든 파일의 diff를 하나의 문자열로 모아 반환한다."""
        cwd = self._work_dir()
        parts: list[str] = []
        for change in self._changes:
            if change.xy == "??":
                continue
            try:
                args = ["git", "diff"]
                if change.staged:
                    args.append("--cached")
                args += ["--", change.path]
                result = _run(args, cwd)
                if result.stdout:
                    parts.append(result.stdout)
            except Exception:
                pass
        return "\n".join(parts)

    def action_close(self) -> None:
        self.post_message(self.CloseRequested())

    # ------------------------------------------------------------------
    # Diff loading
    # ------------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "git-file-list":
            return
        idx = event.list_view.index
        if idx is None or idx >= len(self._changes):
            self._clear_diff()
            return
        self._load_diff(self._changes[idx])

    def _clear_diff(self) -> None:
        try:
            self.query_one("#git-diff-unified", RichLog).clear()
            self.query_one("#git-diff-old", SyncedRichLog).clear()
            self.query_one("#git-diff-new", SyncedRichLog).clear()
            self.query_one("#git-diff-title", Label).update("")
        except Exception:
            pass

    @work(thread=True)
    def _load_diff(self, change: _Change) -> None:
        cwd = self._work_dir()
        unified: list[Text] = []
        left: list[Text] = []
        right: list[Text] = []

        raw_diff = ""
        if change.xy == "??":
            msg = Text("(새 파일 — 추적되지 않음)", style="bright_black")
            unified, left, right = [msg], [msg], [msg]
        else:
            try:
                args = ["git", "diff"]
                if change.staged:
                    args.append("--cached")
                args += ["--", change.path]
                result = _run(args, cwd)
                diff_text = result.stdout
                raw_diff = diff_text
                if not diff_text:
                    msg = Text("(diff 없음)", style="bright_black")
                    unified, left, right = [msg], [msg], [msg]
                else:
                    unified = _to_unified(diff_text)
                    left, right = _to_split(diff_text)
            except Exception as exc:
                err = Text(f"오류: {exc}", style="red")
                unified, left, right = [err], [err], [err]

        self._current_diff_text = raw_diff
        self._current_diff_path = change.path
        path = change.path
        diff_mode = self._diff_mode

        def _update() -> None:
            try:
                self.query_one("#git-diff-title", Label).update(
                    self._diff_title_text(path)
                )
                u_log = self.query_one("#git-diff-unified", RichLog)
                u_log.clear()
                for t in unified:
                    u_log.write(t)
                old_log = self.query_one("#git-diff-old", SyncedRichLog)
                new_log = self.query_one("#git-diff-new", SyncedRichLog)
                old_log.clear()
                new_log.clear()
                for t in left:
                    old_log.write(t)
                for t in right:
                    new_log.write(t)
            except Exception:
                pass

        self.app.call_from_thread(_update)

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
        elif event.button.id == "btn-review":
            self.action_review()
