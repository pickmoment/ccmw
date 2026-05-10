"""SessionPanel — Claude Code 세션 목록 위젯."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.events import Click
from textual.message import Message
from textual.widgets import Label, ListItem, ListView

from ccmw.session_manager import Session, SessionManager


_STATUS_ICON = {
    "busy":    "[green]●[/green]",
    "idle":    "[yellow]○[/yellow]",
    "stopped": "[bright_black]·[/bright_black]",
}


class SessionPanel(Container):
    """저장된 Claude Code 세션 목록을 표시하고 재진입할 수 있는 패널."""

    BINDINGS = [
        Binding("r", "refresh", "새로고침", show=True),
        Binding("enter", "open", "세션 열기", show=True, priority=True),
        Binding("ctrl+k", "remove", "목록 제거", show=True),
        Binding("escape", "close", "닫기", show=False, priority=True),
    ]

    class SessionSelected(Message):
        def __init__(self, session: Session) -> None:
            super().__init__()
            self.session = session

    class CloseRequested(Message):
        """세션 패널을 닫아달라는 요청."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._manager = manager
        self._sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        yield Label("Claude 세션", id="session-list-title")
        yield ListView(id="session-list-view")

    def on_mount(self) -> None:
        self.action_refresh()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._sessions = self._manager.list()
        lv: ListView = self.query_one("#session-list-view", ListView)
        lv.clear()
        if not self._sessions:
            lv.append(ListItem(Label("(세션 없음)")))
            return
        for s in self._sessions:
            lv.append(ListItem(Label(_format_session(s))))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()

    def on_click(self, event: Click) -> None:
        if event.chain >= 2:
            s = self._current()
            if s:
                self.post_message(self.SessionSelected(s))

    def action_open(self) -> None:
        s = self._current()
        if s:
            self.post_message(self.SessionSelected(s))

    def action_close(self) -> None:
        self.post_message(self.CloseRequested())

    def focus_list(self) -> None:
        lv: ListView = self.query_one("#session-list-view", ListView)
        lv.focus()

    def action_remove(self) -> None:
        """목록에서만 제거 (세션 파일은 건드리지 않음)."""
        lv: ListView = self.query_one("#session-list-view", ListView)
        idx = lv.index
        if idx is None or not self._sessions:
            return
        try:
            self._sessions.pop(idx)
        except IndexError:
            return
        lv.clear()
        for s in self._sessions:
            lv.append(ListItem(Label(_format_session(s))))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current(self) -> Session | None:
        lv: ListView = self.query_one("#session-list-view", ListView)
        idx = lv.index
        if idx is None or not self._sessions:
            return None
        try:
            return self._sessions[idx]
        except IndexError:
            return None

    @property
    def selected_session(self) -> Session | None:
        return self._current()


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

def _format_session(s: Session) -> str:
    icon = _STATUS_ICON.get(s.status, "[bright_black]·[/bright_black]")
    title = s.ai_title or s.last_prompt or s.session_id[:7]
    age = _time_ago(s.timestamp)
    short_id = s.session_id[:7]
    return f"{icon} {title}  [dim]{age} · {short_id}[/dim]"


def _time_ago(iso: str) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        diff = datetime.now(tz=timezone.utc) - dt
        secs = int(diff.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "?"
