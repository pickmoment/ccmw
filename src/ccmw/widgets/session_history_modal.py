"""SessionHistoryModal — 세션 대화 이력을 보여주는 모달."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, RichLog

from ccmw.session_manager import Session
from ccmw.widgets.claude_review_modal import ClaudeReviewModal


def _load_messages(session: Session, *, filter_meta: bool = True) -> list[tuple[str, str]]:
    """JSONL 파일에서 (role, text) 튜플 목록 반환.

    filter_meta=True 이면 내부 명령/메타 메시지를 제외한다.
    """
    project_root = Path.home() / ".claude" / "projects"
    cwd = Path(session.cwd)
    encoded = str(cwd).replace("/", "-")
    jsonl_path = project_root / encoded / f"{session.session_id}.jsonl"

    if not jsonl_path.exists():
        return []

    messages: list[tuple[str, str]] = []
    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue

            role = d.get("type")
            if role not in ("user", "assistant"):
                continue
            if d.get("isMeta") and filter_meta:
                continue

            msg = d.get("message", {})
            content = msg.get("content", "")

            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        t = block.get("text", "").strip()
                        if t:
                            parts.append(t)
                text = "\n".join(parts)
            else:
                continue

            if not text:
                continue

            if filter_meta and role == "user" and (
                "<local-command-caveat>" in text
                or "<command-name>" in text
                or text.startswith("<")
            ):
                continue

            messages.append((role, text))
    except Exception:
        pass

    return messages


def _messages_to_text(messages: list[tuple[str, str]]) -> str:
    """(role, text) 목록을 복사용 평문으로 변환한다."""
    parts: list[str] = []
    for role, text in messages:
        label = "사용자" if role == "user" else "Claude"
        parts.append(f"[{label}]\n{text}")
    return "\n\n" + ("\n\n" + "─" * 60 + "\n\n").join(parts) + "\n"


def _copy_to_clipboard(text: str) -> bool:
    """시스템 클립보드에 텍스트를 복사한다. 성공 시 True 반환."""
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
        return True
    except FileNotFoundError:
        pass
    try:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True, timeout=5)
        return True
    except FileNotFoundError:
        pass
    try:
        subprocess.run(["xsel", "--clipboard", "--input"], input=text.encode("utf-8"), check=True, timeout=5)
        return True
    except Exception:
        pass
    return False


class SessionHistoryModal(ModalScreen):
    """세션 대화 이력을 전체 화면 모달로 표시한다."""

    DEFAULT_CSS = """
    SessionHistoryModal {
        align: center middle;
    }
    SessionHistoryModal > Vertical {
        width: 90%;
        height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 0;
    }
    #history-modal-title {
        width: 100%;
        height: 1;
        background: $boost;
        color: $primary;
        text-style: bold;
        padding: 0 2;
    }
    #history-modal-statusbar {
        width: 100%;
        height: 1;
        background: $surface;
        padding: 0 2;
    }
    #history-log {
        width: 100%;
        height: 1fr;
        background: $surface-darken-1;
        padding: 0 1;
        scrollbar-color: $accent 50%;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "닫기", show=False),
        Binding("q", "dismiss", "닫기", show=False),
        Binding("f", "toggle_filter", "메타 필터", show=True),
        Binding("c", "copy", "복사", show=True),
        Binding("a", "analyze", "AI 분석", show=True),
    ]

    def __init__(self, session: Session, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session
        self._filter_meta: bool = True
        self._messages: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        title = self._session.ai_title or self._session.last_prompt or self._session.session_id[:7]
        with Vertical():
            yield Label(f"대화 이력 — {title}", id="history-modal-title")
            yield Label("", id="history-modal-statusbar")
            yield RichLog(id="history-log", markup=False, highlight=False, wrap=True, auto_scroll=False)

    def on_mount(self) -> None:
        self._reload()

    def _reload(self) -> None:
        self._messages = _load_messages(self._session, filter_meta=self._filter_meta)
        self._render_log()
        self._update_statusbar()

    def _render_log(self) -> None:
        log = self.query_one("#history-log", RichLog)
        log.clear()

        if not self._messages:
            log.write(Text("(대화 이력 없음)", style="bright_black"))
            return

        for role, text in self._messages:
            if role == "user":
                log.write(Text("▶ 사용자", style="bold cyan"))
                log.write(Text(text, style="white"))
            else:
                log.write(Text("◆ Claude", style="bold green"))
                log.write(Text(text, style="bright_white"))
            log.write(Text("─" * 60, style="bright_black"))

        log.scroll_home(animate=False)

    def _update_statusbar(self) -> None:
        filter_state = "[green]ON[/green]" if self._filter_meta else "[red]OFF[/red]"
        count = len(self._messages)
        sb = self.query_one("#history-modal-statusbar", Label)
        sb.update(f"f: 메타 필터 {filter_state}  c: 복사  a: AI 분석  메시지 {count}개  ESC: 닫기")

    def action_toggle_filter(self) -> None:
        self._filter_meta = not self._filter_meta
        self._reload()

    def action_analyze(self) -> None:
        if not self._messages:
            self.app.notify("분석할 대화 내용이 없습니다", severity="warning")
            return

        conversation = _messages_to_text(self._messages)
        title = self._session.ai_title or self._session.last_prompt or self._session.session_id[:7]
        prompt = (
            "다음은 Claude Code와의 대화 기록입니다.\n\n"
            f"{conversation}\n\n"
            "이 대화를 분석해서 다음 항목을 한국어로 작성해 주세요:\n\n"
            "## 1. 잘한 점\n"
            "사용자가 Claude를 효과적으로 활용한 부분을 구체적으로 설명해 주세요.\n\n"
            "## 2. 아쉬운 점\n"
            "더 나은 결과를 얻을 수 있었던 부분과 그 이유를 설명해 주세요.\n\n"
            "## 3. 더 잘 활용하기 위한 가이드\n"
            "이 대화를 바탕으로 Claude Code를 더 효과적으로 사용하는 구체적인 팁과 예시를 제시해 주세요."
        )
        cwd = Path(self._session.cwd) if self._session.cwd else Path.home()
        self.app.push_screen(
            ClaudeReviewModal(
                diff_text="",
                title=f"AI 분석 — {title}",
                cwd=cwd,
                initial_prompt=prompt,
            )
        )

    def action_copy(self) -> None:
        if not self._messages:
            self.app.notify("복사할 내용이 없습니다", severity="warning")
            return
        text = _messages_to_text(self._messages)
        if _copy_to_clipboard(text):
            self.app.notify(f"클립보드에 복사됨 ({len(self._messages)}개 메시지)", timeout=2)
        else:
            self.app.notify("클립보드 복사 실패 (pbcopy/xclip/xsel 필요)", severity="error")
