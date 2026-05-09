"""FileBrowser widget — DirectoryTree with hidden-file toggle and git status."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DirectoryTree
from textual.widgets._directory_tree import DirEntry
from textual.widgets._tree import Tree
from textual.worker import Worker, WorkerState

# Highest priority listed first (lower index = higher priority)
_STATUS_PRIORITY = ["D", "U", "M", "R", "C", "A", "?"]

_STATUS_COLOR: dict[str, str] = {
    "M": "yellow",
    "A": "green",
    "?": "bright_black",
    "D": "red",
    "R": "cyan",
    "C": "cyan",
    "U": "magenta",
}


def _priority(code: str) -> int:
    try:
        return _STATUS_PRIORITY.index(code)
    except ValueError:
        return len(_STATUS_PRIORITY)


class FileBrowser(DirectoryTree):
    """A DirectoryTree with hidden-file toggle and git status markers."""

    show_hidden: reactive[bool] = reactive(False)

    class GitStatusUpdated(Message):
        """Posted after a background git-status refresh completes."""

        def __init__(self, branch: str, status: dict[Path, str]) -> None:
            super().__init__()
            self.branch = branch
            self.status = status

    def __init__(self, path: str | Path = ".", **kwargs) -> None:
        super().__init__(path, **kwargs)
        self.current_dir: Path = Path(path).expanduser().resolve()
        self._git_root: Path | None = None
        self._git_status: dict[Path, str] = {}
        self._git_branch: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.refresh_git_status()

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter_paths(self, paths: list[Path]) -> list[Path]:
        if self.show_hidden:
            return list(paths)
        return [p for p in paths if not p.name.startswith(".")]

    # ------------------------------------------------------------------
    # Git status
    # ------------------------------------------------------------------

    def refresh_git_status(self) -> None:
        """Kick off a background thread to fetch git status."""
        cwd = self.current_dir
        self.run_worker(
            lambda: self._fetch_git_status(cwd),
            thread=True,
            exclusive=True,
            group="git",
            exit_on_error=False,
        )

    @staticmethod
    def _fetch_git_status(cwd: Path) -> tuple[Path | None, str, dict[Path, str]]:
        """Run git in a thread; return (repo_root, branch, status_map)."""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if r.returncode != 0:
                return None, "", {}
            root = Path(r.stdout.strip())
        except Exception:
            return None, "", {}

        try:
            br = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if br.returncode == 0:
                branch = br.stdout.strip()
            else:
                # New repo with no commits — HEAD is a symbolic ref but unresolvable
                sym = subprocess.run(
                    ["git", "symbolic-ref", "--short", "HEAD"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                branch = sym.stdout.strip() if sym.returncode == 0 else ""
        except Exception:
            branch = ""

        try:
            st = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if st.returncode != 0:
                return root, branch, {}

            status_map: dict[Path, str] = {}
            for entry in st.stdout.split("\0"):
                if len(entry) < 4:
                    continue
                xy, filepath = entry[:2], entry[3:]
                if not filepath:
                    continue
                x, y = xy[0], xy[1]
                if x == "?" and y == "?":
                    code = "?"
                elif x == "D" or y == "D":
                    code = "D"
                elif x == "A":
                    code = "A"
                elif x in "RCU":
                    code = x
                elif x == "M" or y == "M":
                    code = "M"
                else:
                    code = x if x != " " else y
                    if code == " ":
                        continue
                status_map[root / filepath] = code

            # Roll up to parent directories (highest-priority code wins)
            dir_codes: dict[Path, str] = {}
            for path, code in list(status_map.items()):
                parent = path.parent
                while True:
                    existing = dir_codes.get(parent)
                    if existing is None or _priority(code) < _priority(existing):
                        dir_codes[parent] = code
                    if parent == root:
                        break
                    next_parent = parent.parent
                    if not str(next_parent).startswith(str(root)):
                        break
                    parent = next_parent

            for path, code in dir_codes.items():
                status_map.setdefault(path, code)

            return root, branch, status_map
        except Exception:
            return root, branch, {}

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "git":
            return
        if event.state == WorkerState.SUCCESS:
            result = event.worker.result
            if isinstance(result, tuple) and len(result) == 3:
                root, branch, status = result
                self._git_root = root
                self._git_branch = branch
                self._git_status = status
                self.post_message(self.GitStatusUpdated(branch, status))
                self.refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_label(self, node: object, base_style: object, style: object) -> Text:
        text = super().render_label(node, base_style, style)  # type: ignore[arg-type]
        if not self._git_status:
            return text
        try:
            data = node.data  # type: ignore[union-attr]
            if isinstance(data, DirEntry):
                code = self._git_status.get(data.path)
                if code:
                    color = _STATUS_COLOR.get(code, "white")
                    text = Text(f"{code} ", style=color) + text
        except Exception:
            pass
        return text

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def watch_show_hidden(self, show_hidden: bool) -> None:  # noqa: FBT001
        await self.reload()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        self.current_dir = event.path
        self.refresh_git_status()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        try:
            data = event.node.data
            if isinstance(data, DirEntry):
                path = data.path
                self.current_dir = path if path.is_dir() else path.parent
        except Exception:
            pass
