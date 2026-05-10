"""ClaudeReviewModal — claude -p 를 이용한 diff 리뷰 및 대화 모달."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, RichLog


class ClaudeReviewModal(ModalScreen):
    """claude -p 스트리밍으로 diff 리뷰를 보여주고 추가 질문을 이어받는 모달."""

    BINDINGS = [
        Binding("c", "copy", "복사", show=True),
        Binding("escape", "dismiss", "닫기", show=True),
    ]

    DEFAULT_CSS = """
    ClaudeReviewModal {
        align: center middle;
    }
    ClaudeReviewModal > Vertical {
        width: 90%;
        height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 0;
    }
    #review-title {
        width: 100%;
        height: 1;
        background: $boost;
        color: $primary;
        padding: 0 2;
        text-style: bold;
    }
    #review-log {
        width: 100%;
        height: 1fr;
        background: $surface-darken-1;
        padding: 0 1;
        scrollbar-color: $accent 50%;
    }
    #review-status {
        width: 100%;
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 2;
        text-align: center;
    }
    #review-input {
        width: 100%;
        height: 3;
        background: $surface-darken-1;
        border: tall $surface-lighten-2;
        margin: 0;
    }
    #review-input:focus {
        border: tall $primary;
    }
    """

    def __init__(self, diff_text: str, title: str, cwd: Path) -> None:
        super().__init__()
        self._diff_text = diff_text
        self._title = title
        self._cwd = cwd
        self._session_id: str | None = None
        self._running = False
        self._line_buf = ""
        self._full_text = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"✦ AI 리뷰: {self._title}  [ESC: 닫기]",
                id="review-title",
            )
            yield RichLog(
                id="review-log",
                markup=False,
                highlight=False,
                wrap=True,
            )
            yield Label("⏳ 리뷰 생성 중...", id="review-status")
            yield Input(
                placeholder="추가 질문 입력 후 Enter  (ESC: 닫기)",
                id="review-input",
                disabled=True,
            )

    def on_mount(self) -> None:
        self._start_review()

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    @work(thread=True)
    def _start_review(self) -> None:
        prompt = (
            "아래 코드 변경 사항들을 리뷰하고 테크니컬 레포트를 작성해 주세요.\n"
            "관련 내용을 잘 모르는 사람도 이해할 수 있도록 쉽게 설명해 주세요.\n\n"
            f"```diff\n{self._diff_text}\n```\n\n"
            "다음 항목들을 포함해서 한국어로 작성해 주세요:\n"
            "1. 전체 변경 사항 요약\n"
            "2. 파일별 주요 변경 내용 상세 설명\n"
            "3. 잠재적 이슈 또는 주의점\n"
            "4. 전체적인 평가"
        )
        self._run_claude(prompt)

    @work(thread=True)
    def _start_follow_up(self, question: str) -> None:
        self._run_claude(question)

    # ------------------------------------------------------------------
    # Claude 실행 (blocking — worker thread 에서만 호출)
    # ------------------------------------------------------------------

    def _run_claude(self, prompt: str) -> None:
        """claude -p 로 프롬프트를 실행하고 결과를 스트리밍으로 표시한다."""
        self._running = True
        self._line_buf = ""
        self.app.call_from_thread(self._set_status, "⏳ Claude가 응답 중...")
        self.app.call_from_thread(self._set_input_disabled, True)

        args = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if self._session_id:
            args += ["--resume", self._session_id]
        args.append(prompt)

        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._cwd),
            )

            for raw_line in proc.stdout:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    # JSON이 아닌 경우 평문으로 표시
                    self._buffer_text(raw_line + "\n")
                    continue

                evt_type = event.get("type", "")

                if evt_type == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if sid:
                        self._session_id = sid

                elif evt_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            self._buffer_text(text)

                elif evt_type == "assistant":
                    # 스트림 내 완전한 메시지 블록 처리
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                self._buffer_text(text)

                elif evt_type == "result":
                    sid = event.get("session_id")
                    if sid:
                        self._session_id = sid
                    self._flush_buf()

            proc.wait()

            if proc.returncode != 0:
                stderr = proc.stderr.read().strip()
                if stderr:
                    self.app.call_from_thread(
                        self._write_line,
                        f"⚠️  오류: {stderr}",
                    )

        except FileNotFoundError:
            self.app.call_from_thread(
                self._write_line,
                "⚠️  'claude' 명령어를 찾을 수 없습니다. Claude Code가 설치되어 있는지 확인하세요.",
            )
        except Exception as exc:
            self.app.call_from_thread(self._write_line, f"⚠️  오류: {exc}")

        self._flush_buf()
        self._running = False
        self.app.call_from_thread(
            self._set_status,
            "✓ 완료  |  추가 질문을 입력하고 Enter  |  ESC: 닫기",
        )
        self.app.call_from_thread(self._set_input_disabled, False)
        self.app.call_from_thread(self._focus_input)

    # ------------------------------------------------------------------
    # 텍스트 버퍼링 (worker thread 에서 호출)
    # ------------------------------------------------------------------

    def _buffer_text(self, text: str) -> None:
        """줄 단위로 텍스트를 버퍼링하여 RichLog에 기록한다."""
        self._line_buf += text
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            self.app.call_from_thread(self._write_line, line)

    def _flush_buf(self) -> None:
        if self._line_buf:
            buf = self._line_buf
            self._line_buf = ""
            self.app.call_from_thread(self._write_line, buf)

    # ------------------------------------------------------------------
    # UI 업데이트 (main thread 에서 호출)
    # ------------------------------------------------------------------

    def _write_line(self, line: str) -> None:
        try:
            self.query_one("#review-log", RichLog).write(Text(line))
            self._full_text += line + "\n"
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#review-status", Label).update(text)
        except Exception:
            pass

    def _set_input_disabled(self, disabled: bool) -> None:
        try:
            self.query_one("#review-input", Input).disabled = disabled
        except Exception:
            pass

    def _focus_input(self) -> None:
        try:
            self.query_one("#review-input", Input).focus()
        except Exception:
            pass

    def action_copy(self) -> None:
        text = self._full_text.strip()
        if not text:
            self.app.notify("복사할 내용이 없습니다", severity="warning")
            return
        try:
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            self.app.notify("클립보드에 복사됐습니다", timeout=2)
        except FileNotFoundError:
            try:
                subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True)
                self.app.notify("클립보드에 복사됐습니다", timeout=2)
            except Exception as exc:
                self.app.notify(f"복사 실패: {exc}", severity="error")
        except Exception as exc:
            self.app.notify(f"복사 실패: {exc}", severity="error")

    # ------------------------------------------------------------------
    # 추가 질문 처리
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "review-input" or self._running:
            return
        question = event.input.value.strip()
        if not question:
            return
        self._running = True
        event.input.value = ""
        event.input.disabled = True
        separator = "─" * 60
        log = self.query_one("#review-log", RichLog)
        log.write(Text(separator, style="bright_black"))
        log.write(Text(f"❓ {question}", style="bold cyan"))
        self._full_text += separator + "\n"
        self._full_text += f"❓ {question}\n"
        self._start_follow_up(question)
