"""MultiTerminalPanel — 탭으로 여러 Claude 세션을 관리하는 위젯."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import TabPane, TabbedContent

from ccmw.widgets.terminal_panel import TerminalPanel


class MultiTerminalPanel(Widget):
    """여러 TerminalPanel을 TabbedContent로 관리한다."""

    DEFAULT_CSS = """
    MultiTerminalPanel {
        width: 1fr;
        height: 1fr;
    }
    MultiTerminalPanel TabbedContent {
        height: 1fr;
    }
    MultiTerminalPanel TabPane {
        padding: 0;
        height: 1fr;
    }
    MultiTerminalPanel ContentSwitcher {
        height: 1fr;
    }
    """

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._count = 0

    def compose(self) -> ComposeResult:
        self._count = 1
        with TabbedContent(id="terminal-tabs", initial="pane-1"):
            with TabPane("Claude 1  ×", id="pane-1"):
                yield TerminalPanel(id="tp-1")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _active_terminal(self) -> TerminalPanel | None:
        try:
            tabs = self.query_one("#terminal-tabs", TabbedContent)
            pane_id = tabs.active
            if not pane_id:
                return None
            pane = tabs.get_pane(pane_id)
            return pane.query_one(TerminalPanel)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, cwd: Path | str, command: list[str] | None = None) -> None:
        """활성 탭에서 명령을 실행한다."""
        panel = self._active_terminal()
        if panel:
            panel.start(Path(cwd), command or ["claude"])

    def focus_active(self) -> None:
        """활성 터미널 패널에 포커스를 준다."""
        panel = self._active_terminal()
        if panel:
            panel.focus()

    async def new_tab(
        self,
        cwd: Path | str | None = None,
        command: list[str] | None = None,
        tab_type: str = "Claude",
    ) -> TerminalPanel:
        """새 탭을 추가하고 선택적으로 명령을 실행한다."""
        self._count += 1
        n = self._count
        panel = TerminalPanel(id=f"tp-{n}")
        pane = TabPane(f"{tab_type} {n}  ×", panel, id=f"pane-{n}")
        tabs = self.query_one("#terminal-tabs", TabbedContent)
        await tabs.add_pane(pane)
        tabs.active = f"pane-{n}"
        if cwd:
            panel.start(Path(cwd), command or ["claude"])
        return panel

    async def close_active(self) -> None:
        """활성 탭을 닫는다 (탭이 1개이면 세션만 종료하고 패널을 초기화한다)."""
        tabs = self.query_one("#terminal-tabs", TabbedContent)
        all_panes = list(tabs.query(TabPane))
        if len(all_panes) <= 1:
            panel = self._active_terminal()
            if panel:
                panel.reset()
            return
        pane_id = tabs.active
        if pane_id:
            await tabs.remove_pane(pane_id)
