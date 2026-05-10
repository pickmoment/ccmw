"""GitPanel — VS Code 스타일 git 변경사항 패널."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog
from textual import work

from ccmw.widgets.claude_review_modal import ClaudeReviewModal


class ConfirmModal(ModalScreen[bool]):
    """위험한 작업 전 확인을 요청하는 모달."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $error;
    }
    #confirm-title {
        width: 100%;
        text-align: center;
        color: $error;
        text-style: bold;
        margin-bottom: 1;
    }
    #confirm-message {
        width: 100%;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
        text-align: center;
        color: $text;
    }
    #confirm-buttons {
        width: 100%;
        height: 3;
        align: center middle;
    }
    #confirm-buttons Button {
        margin: 0 1;
        min-width: 16;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", show=False),
        Binding("escape", "cancel", show=False),
        Binding("n", "cancel", show=False),
    ]

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label("⚠ 확인 필요", id="confirm-title")
            yield Label(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button("취소 (n/ESC)", id="btn-cancel-confirm")
                yield Button("확인 (y)", id="btn-ok-confirm", variant="error")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-ok-confirm")


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


@dataclass
class _Branch:
    name: str
    is_current: bool
    is_remote: bool = field(default=False)


@dataclass
class _Commit:
    hash: str
    short_hash: str
    date: str
    author: str
    message: str
    refs: str


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

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


def _get_remote_status(cwd: Path) -> tuple[int, int, bool]:
    """(ahead, behind, has_upstream) 반환. upstream 없으면 has_upstream=False."""
    try:
        r = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
        if r.returncode != 0:
            return 0, 0, False
        r2 = _run(["git", "rev-list", "--count", "--left-right", "HEAD...@{u}"], cwd)
        if r2.returncode != 0:
            return 0, 0, True
        parts = r2.stdout.strip().split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1]), True
        return 0, 0, True
    except Exception:
        return 0, 0, False


def _list_branches(cwd: Path) -> list[_Branch]:
    """로컬 및 원격 브랜치 목록 반환. 로컬 먼저, 원격 뒤."""
    branches: list[_Branch] = []
    try:
        # 로컬 브랜치 (현재 브랜치 표시 포함)
        r = _run(["git", "branch", "--format=%(refname:short)|%(HEAD)"], cwd)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "|" not in line:
                    continue
                name, head = line.split("|", 1)
                branches.append(_Branch(
                    name=name.strip(),
                    is_current=head.strip() == "*",
                    is_remote=False,
                ))
        # 원격 브랜치 (HEAD 포인터 제외)
        r2 = _run(["git", "branch", "-r", "--format=%(refname:short)"], cwd)
        if r2.returncode == 0:
            for line in r2.stdout.splitlines():
                name = line.strip()
                if not name or "HEAD" in name:
                    continue
                branches.append(_Branch(name=name, is_current=False, is_remote=True))
    except Exception:
        pass
    return branches


def _get_log(cwd: Path, max_count: int = 200) -> list[_Commit]:
    try:
        sep = "\x1f"
        fmt = sep.join(["%H", "%h", "%ad", "%an", "%s", "%D"])
        r = _run(
            ["git", "log", f"--format={fmt}", "--date=format:%m-%d", f"-{max_count}"],
            cwd, timeout=10,
        )
        if r.returncode != 0:
            return []
        commits: list[_Commit] = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(sep)
            if len(parts) < 5:
                continue
            commits.append(_Commit(
                hash=parts[0].strip(),
                short_hash=parts[1].strip(),
                date=parts[2].strip(),
                author=parts[3].strip(),
                message=parts[4].strip(),
                refs=parts[5].strip() if len(parts) > 5 else "",
            ))
        return commits
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


# ---------------------------------------------------------------------------
# GitPanel
# ---------------------------------------------------------------------------

class GitPanel(Container):
    """Git 변경사항 보기, 스테이징, 커밋, 동기화, 브랜치 관리 패널."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "refresh", "새로고침", show=True, priority=True),
        Binding("space", "toggle_stage", "스테이지 토글", show=True),
        Binding("a", "stage_all", "모두 스테이지", show=True),
        Binding("x", "restore", "변경 되돌리기", show=True),
        Binding("d", "toggle_diff_mode", "Diff 모드", show=True),
        Binding("p", "pull", "Pull", show=True, priority=True),
        Binding("u", "push", "Push", show=True, priority=True),
        Binding("b", "toggle_branches", "브랜치", show=True, priority=True),
        Binding("h", "toggle_history", "히스토리", show=True, priority=True),
        Binding("n", "new_branch_input", "새 브랜치", show=False),
        Binding("ctrl+d", "delete_branch", "브랜치 삭제", show=False),
        Binding("i", "init", "git init", show=False),
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
        self._diff_mode: str = "split"  # "unified" | "split"
        self._current_diff_text: str = ""
        self._current_diff_path: str = ""
        self._ahead: int = 0
        self._behind: int = 0
        self._has_upstream: bool = False
        self._branch_view: bool = False
        self._branches: list[_Branch] = []
        self._history_view: bool = False
        self._commits: list[_Commit] = []

    def compose(self) -> ComposeResult:
        yield Label("git", id="git-panel-title")
        with Horizontal(id="git-panel-body"):
            with Vertical(id="git-panel-left"):
                # 변경사항 뷰
                with Vertical(id="changes-view"):
                    yield ListView(id="git-file-list")
                    yield Input(placeholder="커밋 메시지...", id="git-commit-msg")
                    with Horizontal(id="git-stage-row"):
                        yield Button("+ 스테이지", id="btn-stage-all")
                        yield Button("커밋", id="btn-commit", variant="primary")
                        yield Button("↺ 복원", id="btn-restore", variant="error")
                    with Horizontal(id="git-remote-row"):
                        yield Button("↓ Pull", id="btn-pull", variant="primary")
                        yield Button("↑ Push", id="btn-push", variant="warning")
                    yield Button("✦ AI 리뷰", id="btn-review", variant="success")
                    yield Button("⊕ git init", id="btn-git-init", variant="warning", classes="hidden")
                # 브랜치 뷰 (기본 숨김)
                with Vertical(id="branches-view", classes="hidden"):
                    yield ListView(id="branch-list")
                    yield Input(placeholder="새 브랜치 이름... (Enter: 생성)", id="branch-name-input")
                    with Horizontal(id="git-branch-buttons"):
                        yield Button("체크아웃", id="btn-checkout", variant="primary")
                        yield Button("생성", id="btn-create-branch", variant="success")
                        yield Button("삭제", id="btn-delete-branch", variant="error")
                # 히스토리 뷰 (기본 숨김)
                with Vertical(id="history-view", classes="hidden"):
                    yield ListView(id="commit-list")
                    with Horizontal(id="git-history-buttons"):
                        yield Button("Soft", id="btn-reset-soft")
                        yield Button("Mixed", id="btn-reset-mixed", variant="warning")
                        yield Button("Hard!", id="btn-reset-hard", variant="error")
                        yield Button("Revert", id="btn-revert", variant="primary")
                        yield Button("✦ AI 리뷰", id="btn-review-commit", variant="success")
            with Vertical(id="git-diff-pane"):
                yield Label("", id="git-diff-title")
                yield RichLog(id="git-diff-unified", markup=False, highlight=False, wrap=False, auto_scroll=False, classes="hidden")
                with Horizontal(id="git-diff-split"):
                    yield SyncedRichLog(id="git-diff-old", markup=False, highlight=False, wrap=False, auto_scroll=False)
                    yield SyncedRichLog(id="git-diff-new", markup=False, highlight=False, wrap=False, auto_scroll=False)

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
    # Internal helpers
    # ------------------------------------------------------------------

    def _work_dir(self) -> Path:
        return self._root or self._cwd

    @property
    def _in_changes_view(self) -> bool:
        return not self._branch_view and not self._history_view

    def _sync_init_button(self) -> None:
        try:
            btn = self.query_one("#btn-git-init", Button)
            if self._root is None:
                btn.remove_class("hidden")
            else:
                btn.add_class("hidden")
        except Exception:
            pass

    def _rebuild_list(self) -> None:
        lv: ListView = self.query_one("#git-file-list", ListView)
        lv.clear()
        if not self._changes:
            if self._root is None:
                lv.append(ListItem(Label("(Git 저장소 없음  —  i 키로 git init)")))
            else:
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

    def _rebuild_branch_list(self) -> None:
        lv: ListView = self.query_one("#branch-list", ListView)
        lv.clear()
        if not self._branches:
            lv.append(ListItem(Label("(브랜치 없음)")))
            return
        for b in self._branches:
            if b.is_current:
                lv.append(ListItem(Label(Text(f"* {b.name}", style="green bold"))))
            elif b.is_remote:
                lv.append(ListItem(Label(Text(f"  {b.name}", style="dim"))))
            else:
                lv.append(ListItem(Label(f"  {b.name}")))

    def _update_title(self) -> None:
        title = self.query_one("#git-panel-title", Label)
        if self._root is None:
            title.update("git  (저장소 없음)")
        elif self._history_view:
            total = len(self._commits)
            title.update(
                f"히스토리  커밋 {total}개  "
                "Soft·스테이징 보존  Mixed·워킹트리 보존  Hard·전부 삭제  Revert·역커밋  h/ESC·뒤로"
            )
        elif self._branch_view:
            current = next((b.name for b in self._branches if b.is_current), "")
            branch_info = f" [{current}]" if current else ""
            title.update(f"브랜치{branch_info}  Enter·체크아웃  n·새 브랜치  Ctrl+D·삭제  b/ESC·뒤로")
        else:
            staged = sum(1 for c in self._changes if c.staged)
            total = len(self._changes)
            remote_parts: list[str] = []
            if self._ahead:
                remote_parts.append(f"⇡{self._ahead}")
            if self._behind:
                remote_parts.append(f"⇣{self._behind}")
            remote = ("  " + " ".join(remote_parts)) if remote_parts else ""
            title.update(f"git  변경 {total}  스테이징됨 {staged}{remote}")

    def _diff_title_text(self, path: str) -> str:
        mode = "split" if self._diff_mode == "split" else "unified"
        return f"{path}  [{mode}]"

    # ------------------------------------------------------------------
    # Actions — 변경사항 뷰
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._changes = _get_status(self._work_dir())
        self._rebuild_list()
        self._update_title()
        self._sync_init_button()
        self._refresh_remote_status()
        if self._branch_view:
            self._load_branches()

    @work(thread=True)
    def _refresh_remote_status(self) -> None:
        ahead, behind, has_upstream = _get_remote_status(self._work_dir())
        def _apply() -> None:
            self._ahead = ahead
            self._behind = behind
            self._has_upstream = has_upstream
            if not self._branch_view:
                self._update_title()
        self.app.call_from_thread(_apply)

    def action_toggle_stage(self) -> None:
        if not self._in_changes_view:
            return
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
        if not self._in_changes_view:
            return
        try:
            _run(["git", "add", "-A"], self._work_dir())
            self.action_refresh()
        except Exception as exc:
            self.app.notify(str(exc), severity="error")

    def action_toggle_diff_mode(self) -> None:
        if self._branch_view:
            return
        if self._diff_mode == "unified":
            self._diff_mode = "split"
            self.query_one("#git-diff-unified").add_class("hidden")
            self.query_one("#git-diff-split").remove_class("hidden")
        else:
            self._diff_mode = "unified"
            self.query_one("#git-diff-split").add_class("hidden")
            self.query_one("#git-diff-unified").remove_class("hidden")
        if self._in_changes_view:
            lv = self.query_one("#git-file-list", ListView)
            idx = lv.index
            if idx is not None and idx < len(self._changes):
                self.query_one("#git-diff-title", Label).update(
                    self._diff_title_text(self._changes[idx].path)
                )
        elif self._history_view:
            commit = self._selected_commit()
            if commit is not None:
                mode = "split" if self._diff_mode == "split" else "unified"
                self.query_one("#git-diff-title", Label).update(
                    f"{commit.short_hash}  {commit.date}  {commit.author}  {commit.message[:50]}  [{mode}]"
                )

    def action_restore(self) -> None:
        """선택한 파일의 워킹 디렉터리 변경사항을 되돌린다 (git restore)."""
        if not self._in_changes_view:
            return
        lv: ListView = self.query_one("#git-file-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._changes):
            return
        change = self._changes[idx]
        if change.xy == "??":
            self.app.notify("추적되지 않는 파일은 복원할 수 없습니다 (git clean 필요)", severity="warning")
            return
        self._do_restore(change.path, change.staged)

    @work(thread=True)
    def _do_restore(self, path: str, staged: bool) -> None:
        cwd = self._work_dir()
        try:
            if staged:
                # 스테이징 해제 후 워킹 디렉터리도 복원
                r1 = subprocess.run(
                    ["git", "restore", "--staged", path],
                    cwd=str(cwd), capture_output=True, text=True, timeout=10,
                )
                if r1.returncode != 0:
                    err = r1.stderr.strip() or r1.stdout.strip()
                    self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
                    return
            result = subprocess.run(
                ["git", "restore", path],
                cwd=str(cwd), capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                self.app.call_from_thread(self.app.notify, f"복원 완료: {path}", timeout=3)
            else:
                err = result.stderr.strip() or result.stdout.strip()
                self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
        finally:
            self.app.call_from_thread(self.action_refresh)

    def action_review(self) -> None:
        if self._history_view:
            commit = self._selected_commit()
            if commit is None:
                self.app.notify("리뷰할 커밋을 선택하세요", severity="warning")
                return
            self._review_commit(commit)
            return
        if not self._in_changes_view:
            return
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

    @work(thread=True)
    def _review_commit(self, commit: _Commit) -> None:
        cwd = self._work_dir()
        try:
            result = _run(["git", "show", "--patch", commit.hash], cwd, timeout=15)
            diff_text = result.stdout
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return
        if not diff_text:
            self.app.call_from_thread(
                self.app.notify, "리뷰할 diff가 없습니다", severity="warning"
            )
            return
        title = f"{commit.short_hash}  {commit.date}  {commit.message[:40]}"

        def _open() -> None:
            self.app.push_screen(
                ClaudeReviewModal(diff_text=diff_text, title=title, cwd=cwd)
            )
        self.app.call_from_thread(_open)

    def _collect_all_diffs(self) -> str:
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

    def action_pull(self) -> None:
        self._do_pull()

    def action_push(self) -> None:
        self._do_push()

    def action_init(self) -> None:
        if self._root is not None:
            self.app.notify("이미 Git 저장소입니다", severity="warning")
            return
        self._do_init()

    @work(thread=True)
    def _do_init(self) -> None:
        cwd = self._cwd
        self.app.call_from_thread(self.app.notify, "git init 실행 중...", timeout=2)
        try:
            result = subprocess.run(
                ["git", "init"], cwd=str(cwd),
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return

        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, f"git init 완료: {cwd.name}", timeout=3)
            def _apply() -> None:
                self._root = _get_git_root(self._cwd)
                self.action_refresh()
            self.app.call_from_thread(_apply)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)

    def action_close(self) -> None:
        if self._history_view:
            self.action_toggle_history()
        elif self._branch_view:
            self.action_toggle_branches()
        else:
            self.post_message(self.CloseRequested())

    # ------------------------------------------------------------------
    # Actions — 브랜치 뷰
    # ------------------------------------------------------------------

    def action_toggle_branches(self) -> None:
        if self._branch_view:
            self._branch_view = False
            self.query_one("#changes-view").remove_class("hidden")
            self.query_one("#branches-view").add_class("hidden")
            self._update_title()
            try:
                self.query_one("#git-file-list", ListView).focus()
            except Exception:
                pass
        else:
            if self._history_view:
                self._history_view = False
                self.query_one("#history-view").add_class("hidden")
            self._branch_view = True
            self.query_one("#changes-view").add_class("hidden")
            self.query_one("#branches-view").remove_class("hidden")
            self._load_branches()
            self._update_title()
            try:
                self.query_one("#branch-list", ListView).focus()
            except Exception:
                pass

    def action_toggle_history(self) -> None:
        if self._history_view:
            self._history_view = False
            self.query_one("#changes-view").remove_class("hidden")
            self.query_one("#history-view").add_class("hidden")
            self._update_title()
            try:
                self.query_one("#git-file-list", ListView).focus()
            except Exception:
                pass
        else:
            if self._branch_view:
                self._branch_view = False
                self.query_one("#branches-view").add_class("hidden")
            self._history_view = True
            self.query_one("#changes-view").add_class("hidden")
            self.query_one("#history-view").remove_class("hidden")
            self._load_commits()
            self._update_title()
            try:
                self.query_one("#commit-list", ListView).focus()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # History actions
    # ------------------------------------------------------------------

    def action_reset_soft(self) -> None:
        if not self._history_view:
            return
        commit = self._selected_commit()
        if commit is None:
            return
        self._do_reset(commit.hash, "soft")

    def action_reset_mixed(self) -> None:
        if not self._history_view:
            return
        commit = self._selected_commit()
        if commit is None:
            return
        self._do_reset(commit.hash, "mixed")

    def action_reset_hard(self) -> None:
        if not self._history_view:
            return
        commit = self._selected_commit()
        if commit is None:
            return
        changed = len(self._changes)
        warn = f"[bold red]Reset --hard[/]\n{commit.short_hash} {commit.message[:40]}\n"
        if changed:
            warn += f"\n워킹 디렉터리의 변경 파일 {changed}개가 [bold]영구 삭제[/]됩니다.\n"
        warn += "\n진행하시겠습니까? (y / n)"
        def _on_confirm(ok: bool | None) -> None:
            if ok:
                self._do_reset(commit.hash, "hard")
        self.app.push_screen(ConfirmModal(warn), _on_confirm)

    def action_revert_commit(self) -> None:
        if not self._history_view:
            return
        commit = self._selected_commit()
        if commit is None:
            return
        self._do_revert(commit.hash)

    def _selected_commit(self) -> _Commit | None:
        lv = self.query_one("#commit-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._commits):
            return None
        return self._commits[idx]

    def _rebuild_commit_list(self) -> None:
        lv: ListView = self.query_one("#commit-list", ListView)
        lv.clear()
        if not self._commits:
            lv.append(ListItem(Label("(커밋 없음)")))
            return
        for c in self._commits:
            refs_part = f" [cyan]({c.refs})[/cyan]" if c.refs else ""
            msg = c.message[:22] + "…" if len(c.message) > 22 else c.message
            text = f"[dim]{c.short_hash}[/dim] [bright_black]{c.date}[/bright_black] {msg}{refs_part}"
            lv.append(ListItem(Label(text)))

    @work(thread=True)
    def _load_commits(self) -> None:
        commits = _get_log(self._work_dir())
        def _apply() -> None:
            self._commits = commits
            self._rebuild_commit_list()
            self._update_title()
        self.app.call_from_thread(_apply)

    @work(thread=True)
    def _load_commit_diff(self, commit: _Commit) -> None:
        cwd = self._work_dir()
        try:
            result = _run(["git", "show", "--stat", "--patch", commit.hash], cwd, timeout=15)
            diff_text = result.stdout
        except Exception as exc:
            diff_text = f"오류: {exc}"
        if diff_text:
            unified = _to_unified(diff_text)
            left, right = _to_split(diff_text)
        else:
            msg = Text("(diff 없음)", style="bright_black")
            unified, left, right = [msg], [msg], [msg]

        def _update() -> None:
            try:
                mode = "split" if self._diff_mode == "split" else "unified"
                self.query_one("#git-diff-title", Label).update(
                    f"{commit.short_hash}  {commit.date}  {commit.author}  {commit.message[:50]}  [{mode}]"
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

    @work(thread=True)
    def _do_reset(self, commit_hash: str, mode: str) -> None:
        cwd = self._work_dir()
        self.app.call_from_thread(self.app.notify, f"git reset --{mode} {commit_hash[:7]}...", timeout=2)
        try:
            result = subprocess.run(
                ["git", "reset", f"--{mode}", commit_hash],
                cwd=str(cwd), capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return
        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, f"Reset --{mode} 완료", timeout=3)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        def _refresh() -> None:
            self._changes = _get_status(self._work_dir())
            self._rebuild_list()
            self._update_title()
            self._load_commits()
        self.app.call_from_thread(_refresh)

    @work(thread=True)
    def _do_revert(self, commit_hash: str) -> None:
        cwd = self._work_dir()
        self.app.call_from_thread(self.app.notify, f"git revert {commit_hash[:7]}...", timeout=2)
        try:
            result = subprocess.run(
                ["git", "revert", "--no-edit", commit_hash],
                cwd=str(cwd), capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return
        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, "Revert 완료 (새 커밋 생성)", timeout=3)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            hint = "  (충돌 시 git revert --abort 로 취소)"
            self.app.call_from_thread(
                self.app.notify, err + hint, severity="error", timeout=8
            )
        def _refresh() -> None:
            self._changes = _get_status(self._work_dir())
            self._rebuild_list()
            self._update_title()
            self._load_commits()
        self.app.call_from_thread(_refresh)

    def action_new_branch_input(self) -> None:
        if not self._branch_view:
            return
        try:
            self.query_one("#branch-name-input", Input).focus()
        except Exception:
            pass

    def action_delete_branch(self) -> None:
        if not self._branch_view:
            return
        self._delete_selected_branch()

    # ------------------------------------------------------------------
    # Branch helpers
    # ------------------------------------------------------------------

    def _checkout_selected(self) -> None:
        lv = self.query_one("#branch-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._branches):
            return
        branch = self._branches[idx]
        if branch.is_current:
            self.app.notify("이미 현재 브랜치입니다", severity="warning")
            return
        self._do_checkout(branch.name, branch.is_remote)

    def _create_branch_from_input(self) -> None:
        name_input = self.query_one("#branch-name-input", Input)
        name = name_input.value.strip()
        if not name:
            self.app.notify("브랜치 이름을 입력하세요", severity="warning")
            name_input.focus()
            return
        name_input.value = ""
        self._do_create_branch(name)

    def _delete_selected_branch(self) -> None:
        lv = self.query_one("#branch-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._branches):
            return
        branch = self._branches[idx]
        if branch.is_current:
            self.app.notify("현재 브랜치는 삭제할 수 없습니다", severity="warning")
            return
        if branch.is_remote:
            self.app.notify("원격 브랜치는 이 패널에서 삭제할 수 없습니다", severity="warning")
            return
        self._do_delete_branch(branch.name)

    # ------------------------------------------------------------------
    # Branch workers
    # ------------------------------------------------------------------

    @work(thread=True)
    def _load_branches(self) -> None:
        branches = _list_branches(self._work_dir())
        def _apply() -> None:
            self._branches = branches
            self._rebuild_branch_list()
            self._update_title()
        self.app.call_from_thread(_apply)

    @work(thread=True)
    def _do_checkout(self, branch_name: str, is_remote: bool) -> None:
        cwd = self._work_dir()
        self.app.call_from_thread(self.app.notify, f"체크아웃: {branch_name}...", timeout=2)
        try:
            if is_remote:
                # "origin/feat" → local "feat" tracking "origin/feat"
                local_name = branch_name.split("/", 1)[-1]
                cmd = ["git", "checkout", "-b", local_name, "--track", branch_name]
            else:
                cmd = ["git", "checkout", branch_name]
            result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self.app.notify, "체크아웃 시간 초과", severity="error")
            return
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return

        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, f"브랜치 전환 완료", timeout=3)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        self.app.call_from_thread(self.action_refresh)

    @work(thread=True)
    def _do_create_branch(self, branch_name: str) -> None:
        cwd = self._work_dir()
        self.app.call_from_thread(self.app.notify, f"브랜치 생성 중: {branch_name}...", timeout=2)
        try:
            result = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(cwd), capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return

        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, f"브랜치 생성 완료: {branch_name}", timeout=3)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        self.app.call_from_thread(self.action_refresh)

    @work(thread=True)
    def _do_delete_branch(self, branch_name: str) -> None:
        cwd = self._work_dir()
        try:
            result = subprocess.run(
                ["git", "branch", "-d", branch_name],
                cwd=str(cwd), capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return

        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, f"브랜치 삭제: {branch_name}", timeout=3)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        self.app.call_from_thread(self.action_refresh)

    # ------------------------------------------------------------------
    # Diff loading (변경사항 뷰 전용)
    # ------------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "commit-list":
            idx = event.list_view.index
            if idx is None or idx >= len(self._commits):
                self._clear_diff()
                return
            self._load_commit_diff(self._commits[idx])
            return
        if event.list_view.id != "git-file-list":
            return
        idx = event.list_view.index
        if idx is None or idx >= len(self._changes):
            self._clear_diff()
            return
        self._load_diff(self._changes[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "branch-list":
            self._checkout_selected()

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
        elif event.input.id == "branch-name-input":
            self._create_branch_from_input()

    # ------------------------------------------------------------------
    # Remote sync (pull / push) — background threads
    # ------------------------------------------------------------------

    @work(thread=True)
    def _do_pull(self) -> None:
        cwd = self._work_dir()
        try:
            self.app.call_from_thread(self.app.notify, "Pull 중...", timeout=2)
            result = subprocess.run(
                ["git", "pull"], cwd=str(cwd),
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                msg = (result.stdout.strip() or "Pull 완료")[:80]
                self.app.call_from_thread(self.app.notify, msg, timeout=3)
            else:
                err = (result.stderr.strip() or result.stdout.strip() or "Pull 실패")
                self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self.app.notify, "Pull 시간 초과", severity="error")
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
        finally:
            self.app.call_from_thread(self.action_refresh)

    @work(thread=True)
    def _do_push(self) -> None:
        cwd = self._work_dir()
        try:
            self.app.call_from_thread(self.app.notify, "Push 중...", timeout=2)
            _, _, has_upstream = _get_remote_status(cwd)
            if not has_upstream:
                r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
                branch = r.stdout.strip() if r.returncode == 0 else "main"
                cmd = ["git", "push", "--set-upstream", "origin", branch]
            else:
                cmd = ["git", "push"]
            result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                self.app.call_from_thread(self.app.notify, "Push 완료", timeout=3)
            else:
                err = (result.stderr.strip() or result.stdout.strip() or "Push 실패")
                self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self.app.notify, "Push 시간 초과", severity="error")
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
        finally:
            self.app.call_from_thread(self.action_refresh)

    # ------------------------------------------------------------------
    # Button handler
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-reset-soft":
            self.action_reset_soft()
        elif event.button.id == "btn-reset-mixed":
            self.action_reset_mixed()
        elif event.button.id == "btn-reset-hard":
            self.action_reset_hard()
        elif event.button.id == "btn-revert":
            self.action_revert_commit()
        elif event.button.id == "btn-review-commit":
            self.action_review()
        elif event.button.id == "btn-stage-all":
            self.action_stage_all()
        elif event.button.id == "btn-commit":
            self._do_commit()
        elif event.button.id == "btn-pull":
            self._do_pull()
        elif event.button.id == "btn-push":
            self._do_push()
        elif event.button.id == "btn-restore":
            self.action_restore()
        elif event.button.id == "btn-review":
            self.action_review()
        elif event.button.id == "btn-git-init":
            self.action_init()
        elif event.button.id == "btn-checkout":
            self._checkout_selected()
        elif event.button.id == "btn-create-branch":
            self._create_branch_from_input()
        elif event.button.id == "btn-delete-branch":
            self._delete_selected_branch()
