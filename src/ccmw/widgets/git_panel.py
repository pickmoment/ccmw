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


@dataclass
class _Branch:
    name: str
    is_current: bool
    is_remote: bool = field(default=False)


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
        Binding("d", "toggle_diff_mode", "Diff 모드", show=True),
        Binding("p", "pull", "Pull", show=True, priority=True),
        Binding("b", "toggle_branches", "브랜치", show=True, priority=True),
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
        self._diff_mode: str = "unified"  # "unified" | "split"
        self._current_diff_text: str = ""
        self._current_diff_path: str = ""
        self._ahead: int = 0
        self._behind: int = 0
        self._has_upstream: bool = False
        self._branch_view: bool = False
        self._branches: list[_Branch] = []

    def compose(self) -> ComposeResult:
        yield Label("git", id="git-panel-title")
        with Horizontal(id="git-panel-body"):
            with Vertical(id="git-panel-left"):
                # 변경사항 뷰
                with Vertical(id="changes-view"):
                    yield ListView(id="git-file-list")
                    yield Input(placeholder="커밋 메시지...", id="git-commit-msg")
                    with Horizontal(id="git-stage-row"):
                        yield Button("스테이지 (a)", id="btn-stage-all")
                        yield Button("커밋", id="btn-commit", variant="primary")
                    with Horizontal(id="git-remote-row"):
                        yield Button("Pull ↓ (p)", id="btn-pull")
                        yield Button("Push ↑", id="btn-push", variant="warning")
                    yield Button("AI 리뷰 (v)", id="btn-review", variant="success")
                    yield Button("git init (i)", id="btn-git-init", variant="warning", classes="hidden")
                # 브랜치 뷰 (기본 숨김)
                with Vertical(id="branches-view", classes="hidden"):
                    yield ListView(id="branch-list")
                    yield Input(placeholder="새 브랜치 이름... (Enter: 생성)", id="branch-name-input")
                    with Horizontal(id="git-branch-buttons"):
                        yield Button("체크아웃", id="btn-checkout", variant="primary")
                        yield Button("생성", id="btn-create-branch", variant="success")
                        yield Button("삭제", id="btn-delete-branch", variant="error")
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _work_dir(self) -> Path:
        return self._root or self._cwd

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
        if self._branch_view:
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
        if self._branch_view:
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
        lv = self.query_one("#git-file-list", ListView)
        idx = lv.index
        if idx is not None and idx < len(self._changes):
            self.query_one("#git-diff-title", Label).update(
                self._diff_title_text(self._changes[idx].path)
            )

    def action_review(self) -> None:
        if self._branch_view:
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
        if self._branch_view:
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
            self._branch_view = True
            self.query_one("#changes-view").add_class("hidden")
            self.query_one("#branches-view").remove_class("hidden")
            self._load_branches()
            self._update_title()
            try:
                self.query_one("#branch-list", ListView).focus()
            except Exception:
                pass

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
        self.call_from_thread(self.action_refresh)

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
        self.call_from_thread(self.action_refresh)

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
        self.call_from_thread(self.action_refresh)

    # ------------------------------------------------------------------
    # Diff loading (변경사항 뷰 전용)
    # ------------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
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
        self.app.call_from_thread(self.app.notify, "Pull 중...", timeout=2)
        try:
            result = subprocess.run(
                ["git", "pull"], cwd=str(cwd),
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self.app.notify, "Pull 시간 초과", severity="error")
            return
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return

        if result.returncode == 0:
            msg = (result.stdout.strip() or "Pull 완료")[:80]
            self.app.call_from_thread(self.app.notify, msg, timeout=3)
        else:
            err = result.stderr.strip() or result.stdout.strip()
            self.app.call_from_thread(self.app.notify, err, severity="error", timeout=5)
        self.call_from_thread(self.action_refresh)

    @work(thread=True)
    def _do_push(self) -> None:
        cwd = self._work_dir()
        self.app.call_from_thread(self.app.notify, "Push 중...", timeout=2)
        try:
            _, _, has_upstream = _get_remote_status(cwd)
            if not has_upstream:
                r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
                branch = r.stdout.strip() if r.returncode == 0 else "main"
                cmd = ["git", "push", "--set-upstream", "origin", branch]
            else:
                cmd = ["git", "push"]
            result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self.app.notify, "Push 시간 초과", severity="error")
            return
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return

        if result.returncode == 0:
            self.app.call_from_thread(self.app.notify, "Push 완료", timeout=3)
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
        elif event.button.id == "btn-pull":
            self._do_pull()
        elif event.button.id == "btn-push":
            self._do_push()
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
