"""CCMWApp — Textual TUI entry point for Claude Code Multi-Window."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Label, Static

from ccmw.input_source import get_input_language
from ccmw.session_manager import SessionManager
from ccmw.widgets.file_browser import FileBrowser
from ccmw.widgets.file_viewer import FileViewer
from ccmw.widgets.git_panel import GitPanel
from ccmw.widgets.multi_terminal_panel import MultiTerminalPanel
from ccmw.widgets.session_panel import SessionPanel

class NewTabPicker(ModalScreen):
    """Claude 또는 Shell 탭 선택 모달."""

    DEFAULT_CSS = """
    NewTabPicker {
        align: center middle;
    }
    NewTabPicker > Vertical {
        width: 36;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    NewTabPicker #picker-title {
        width: 1fr;
        text-align: center;
        margin-bottom: 1;
        text-style: bold;
        color: $primary;
    }
    NewTabPicker Button {
        width: 1fr;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "닫기", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("새 탭 열기", id="picker-title")
            yield Button("Claude", id="btn-claude", variant="primary")
            yield Button("Shell", id="btn-shell", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class CCMWApp(App):
    """Claude Code Multi-Window TUI application."""

    CSS_PATH = "styles.tcss"
    TITLE = "Claude Code Multi-Window"

    BINDINGS = [
        Binding("q", "quit", "종료"),
        Binding("h", "toggle_hidden", "숨김 토글"),
        Binding("tab", "focus_next", "다음 패널"),
        Binding("shift+tab", "focus_previous", "이전 패널"),
        Binding("ctrl+n", "new_tab", "새 탭"),
        Binding("ctrl+w", "close_terminal", "터미널 닫기"),
        Binding("s", "toggle_sessions", "세션 목록"),
        Binding("r", "refresh_sessions", "새로고침"),
        Binding("e", "toggle_viewer", "뷰어 토글"),
        Binding("g", "toggle_git", "Git"),
    ]

    def __init__(self, start_cwd: str | Path = ".") -> None:
        super().__init__()
        self._start_cwd: Path = Path(start_cwd).expanduser().resolve()
        self._sessions = SessionManager(self._start_cwd)
        self._session_panel_visible: bool = False
        self._git_panel_visible: bool = False
        self._viewer_visible: bool = False
        self._last_lang: str = ""

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            with Vertical(id="file-browser-pane"):
                with Horizontal(id="status-bar"):
                    yield Static("", id="current-dir")
                    yield Static("", id="git-branch")
                    yield Static("EN ", id="input-lang")
                yield FileBrowser(str(self._start_cwd), id="file-browser")
            yield FileViewer(id="file-viewer")
            yield MultiTerminalPanel(id="terminal-panel")
        yield SessionPanel(manager=self._sessions, id="session-panel", classes="hidden")
        yield GitPanel(cwd=self._start_cwd, id="git-panel", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Multi-Window Terminal"
        self.call_after_refresh(self._open_claude_in, self._start_cwd)
        self._update_current_dir(self._start_cwd)
        self._poll_input_lang()
        self.set_interval(0.2, self._poll_input_lang)
        self.set_interval(5.0, self._poll_git_status)

    def _poll_git_status(self) -> None:
        try:
            self.query_one("#file-browser", FileBrowser).refresh_git_status()
        except Exception:
            pass

    def _poll_input_lang(self) -> None:
        lang = get_input_language()
        if lang and lang != self._last_lang:
            self._last_lang = lang
            self._update_input_lang_widget(lang)

    def _update_input_lang_widget(self, lang: str) -> None:
        widget = self.query_one("#input-lang", Static)
        display = "한 " if lang == "한" else "EN "
        widget.update(display)
        widget.remove_class("lang-ko", "lang-en")
        widget.add_class("lang-ko" if lang == "한" else "lang-en")

    def _update_current_dir(self, path: Path) -> None:
        home = Path.home()
        try:
            rel = path.relative_to(home)
            display = "~/" + str(rel) if str(rel) != "." else "~"
        except ValueError:
            display = str(path)
        try:
            self.query_one("#current-dir", Static).update(display)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_directory_tree_directory_selected(self, event) -> None:
        self._update_current_dir(event.path)

    def on_directory_tree_file_selected(
        self, event: FileBrowser.FileSelected
    ) -> None:
        """Forward the selected file path to the FileViewer."""
        self._update_current_dir(event.path.parent)
        file_viewer = self.query_one("#file-viewer", FileViewer)
        file_viewer.show_file(event.path)
        self._open_viewer()

    def on_file_viewer_close_requested(self) -> None:
        """FileViewer의 ESC 키 요청으로 뷰어를 닫는다."""
        self._close_viewer()

    def on_file_browser_git_status_updated(
        self, event: FileBrowser.GitStatusUpdated
    ) -> None:
        widget = self.query_one("#git-branch", Static)
        if event.branch:
            # Exclude directory-rollup entries (paths that are ancestors of other entries)
            paths = set(event.status.keys())
            dirty = sum(
                1 for p in paths
                if not any(str(q).startswith(str(p) + "/") for q in paths)
            )
            suffix = f" +{dirty}" if dirty else ""
            widget.update(f"⎇ {event.branch}{suffix}")
            widget.remove_class("git-clean", "git-dirty")
            widget.add_class("git-dirty" if dirty else "git-clean")
        else:
            widget.update("")
            widget.remove_class("git-clean", "git-dirty")

    def on_session_panel_close_requested(self, _event: SessionPanel.CloseRequested) -> None:
        if self._session_panel_visible:
            self.action_toggle_sessions()

    def on_git_panel_close_requested(self, _event: GitPanel.CloseRequested) -> None:
        if self._git_panel_visible:
            self.action_toggle_git()

    async def on_session_panel_session_selected(self, event: SessionPanel.SessionSelected) -> None:
        """선택한 세션을 새 터미널 탭에서 resume한다."""
        s = event.session
        cwd = s.cwd or str(self._start_cwd)
        try:
            panel = self.query_one("#terminal-panel", MultiTerminalPanel)
            term = await panel.new_tab(cwd, ["claude", "--resume", s.session_id])
            term.focus()
        except Exception as exc:
            self.notify(f"세션 열기 실패: {exc}", severity="error")
        if self._session_panel_visible:
            self.query_one("#session-panel", SessionPanel).add_class("hidden")
            self._session_panel_visible = False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_toggle_viewer(self) -> None:
        if self._viewer_visible:
            self._close_viewer()
        else:
            self._open_viewer()

    def _open_viewer(self) -> None:
        if not self._viewer_visible:
            self.query_one("#main-container").add_class("viewer-open")
            self._viewer_visible = True

    def _close_viewer(self) -> None:
        if self._viewer_visible:
            self.query_one("#main-container").remove_class("viewer-open")
            self._viewer_visible = False

    def action_toggle_hidden(self) -> None:
        file_browser = self.query_one("#file-browser", FileBrowser)
        file_browser.action_toggle_hidden()

    async def action_new_tab(self) -> None:
        """Claude 또는 Shell 선택 모달을 띄운다."""
        async def handle(choice: str | None) -> None:
            if choice == "btn-claude":
                await self.action_new_terminal()
            elif choice == "btn-shell":
                await self.action_new_shell()
        await self.push_screen(NewTabPicker(), handle)

    async def action_new_terminal(self) -> None:
        """새 탭에서 Claude를 실행한다."""
        try:
            fb = self.query_one("#file-browser", FileBrowser)
            target = fb.current_dir
        except Exception:
            target = self._start_cwd
        cwd = target if target.is_dir() else target.parent
        try:
            panel = self.query_one("#terminal-panel", MultiTerminalPanel)
            term = await panel.new_tab(cwd, ["claude"])
            term.focus()
        except Exception as exc:
            self.notify(f"새 Claude 실행 실패: {exc}", severity="error")

    async def action_new_shell(self) -> None:
        """새 탭에서 셸을 실행한다."""
        import os as _os
        shell = _os.environ.get("SHELL", "zsh")
        try:
            fb = self.query_one("#file-browser", FileBrowser)
            target = fb.current_dir
        except Exception:
            target = self._start_cwd
        cwd = target if target.is_dir() else target.parent
        try:
            panel = self.query_one("#terminal-panel", MultiTerminalPanel)
            term = await panel.new_tab(cwd, [shell], tab_type="Shell")
            term.focus()
        except Exception as exc:
            self.notify(f"셸 실행 실패: {exc}", severity="error")

    async def action_close_terminal(self) -> None:
        """활성 터미널 탭을 닫는다."""
        try:
            panel = self.query_one("#terminal-panel", MultiTerminalPanel)
            tabs = panel.query_one("#terminal-tabs")
            tab_count = len(list(tabs.query("TabPane")))
            await panel.close_active()
            if tab_count <= 1:
                self.notify("세션 종료됨", timeout=1.5)
            else:
                self.notify("탭 닫힘", timeout=1.5)
        except Exception:
            pass

    def action_toggle_sessions(self) -> None:
        panel = self.query_one("#session-panel", SessionPanel)
        if self._session_panel_visible:
            panel.add_class("hidden")
            self._session_panel_visible = False
            try:
                self.query_one("#terminal-panel", MultiTerminalPanel).focus_active()
            except Exception:
                pass
        else:
            panel.remove_class("hidden")
            self._session_panel_visible = True
            panel.action_refresh()
            self.call_after_refresh(panel.focus_list)

    async def action_refresh_sessions(self) -> None:
        try:
            fb = self.query_one("#file-browser", FileBrowser)
            await fb.reload()
            fb.refresh_git_status()
        except Exception:
            pass
        if self._session_panel_visible:
            try:
                panel = self.query_one("#session-panel", SessionPanel)
                panel.action_refresh()
            except Exception:
                pass

    def action_toggle_git(self) -> None:
        panel = self.query_one("#git-panel", GitPanel)
        if self._git_panel_visible:
            panel.add_class("hidden")
            self._git_panel_visible = False
            try:
                self.query_one("#terminal-panel", MultiTerminalPanel).focus_active()
            except Exception:
                pass
        else:
            panel.remove_class("hidden")
            self._git_panel_visible = True
            panel.action_refresh()
            self.call_after_refresh(panel.query_one("#git-file-list").focus)

    def action_refresh_git(self) -> None:
        try:
            self.query_one("#file-browser", FileBrowser).refresh_git_status()
        except Exception:
            pass
        if self._git_panel_visible:
            try:
                self.query_one("#git-panel", GitPanel).action_refresh()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_claude_in(self, target: Path) -> None:
        """활성 터미널 탭에서 Claude를 실행한다."""
        cwd = target if target.is_dir() else target.parent
        try:
            panel = self.query_one("#terminal-panel", MultiTerminalPanel)
            panel.start(cwd, ["claude"])
            panel.focus_active()
        except Exception as exc:
            self.notify(f"claude 실행 실패: {exc}", severity="error")
        if self._session_panel_visible:
            try:
                self.query_one("#session-panel", SessionPanel).action_refresh()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for the ccmw command."""
    import argparse

    parser = argparse.ArgumentParser(description="Claude Code Multi-Window TUI")
    parser.add_argument("--cwd", default=".", help="작업 디렉토리")
    parser.add_argument("--version", action="version", version="ccmw 0.1.0")
    args = parser.parse_args()

    app = CCMWApp(start_cwd=args.cwd)
    app.run()


if __name__ == "__main__":
    main()
