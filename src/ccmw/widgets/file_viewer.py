from pathlib import Path

from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

MAX_FILE_SIZE = 1024 * 1024  # 1MB
BINARY_CHECK_BYTES = 1024

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "jsx",
    ".tsx": "tsx",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".fish": "fish",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".xml": "xml",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".lua": "lua",
    ".r": "r",
    ".R": "r",
    ".dockerfile": "dockerfile",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".vim": "vim",
    ".el": "lisp",
    ".clj": "clojure",
    ".hs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".dart": "dart",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".scala": "scala",
    ".pl": "perl",
    ".pm": "perl",
}


def _detect_language(path: Path) -> str:
    """Detect programming language from file extension."""
    suffix = path.suffix.lower()
    # Special case for Dockerfile (no extension)
    if path.name.lower() in ("dockerfile", "containerfile"):
        return "dockerfile"
    return EXTENSION_TO_LANGUAGE.get(suffix, "text")


class FileViewer(Widget):
    """A widget that displays file contents with syntax highlighting."""

    class CloseRequested(Message):
        """파일 뷰어를 닫도록 앱에 요청한다."""

    BINDINGS = [
        Binding("escape", "close", "뷰어 닫기", show=False),
        Binding("w", "toggle_word_wrap", "자동줄바꿈"),
    ]

    DEFAULT_CSS = """
    FileViewer {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.current_path: Path | None = None
        self._content_widget: Static | None = None
        self._word_wrap: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", id="viewer-title")
        with VerticalScroll(id="viewer-scroll"):
            yield Static("파일을 선택하세요", id="file-content")

    def on_mount(self) -> None:
        self._content_widget = self.query_one("#file-content", Static)

    def action_close(self) -> None:
        self.post_message(self.CloseRequested())

    def action_toggle_word_wrap(self) -> None:
        self._word_wrap = not self._word_wrap
        if self._word_wrap:
            self.add_class("word-wrap-on")
        else:
            self.remove_class("word-wrap-on")
        if self.current_path:
            self.show_file(self.current_path)

    def _update_title(self) -> None:
        if self.current_path is None:
            return
        wrap_hint = "  [green]줄바꿈 ON[/]" if self._word_wrap else ""
        self.query_one("#viewer-title", Static).update(
            f" {self.current_path.name}{wrap_hint}  [dim]ESC 닫기  W 줄바꿈[/]"
        )

    def show_file(self, path: Path) -> None:
        """Display the contents of the given file path.

        Handles:
        - Files larger than 1MB: shows an error message
        - Binary files (containing null bytes in first 1024 bytes): shows an error message
        - Text files: renders with Rich Syntax highlighting based on file extension
        - Encoding errors: handled with errors='replace'
        """
        self.current_path = path

        if self._content_widget is None:
            self._content_widget = self.query_one("#file-content", Static)

        self._update_title()

        # Check if file exists
        if not path.exists():
            self._content_widget.update(f"파일을 찾을 수 없습니다: {path}")
            return

        if not path.is_file():
            self._content_widget.update(f"파일이 아닙니다: {path}")
            return

        # Check file size
        try:
            file_size = path.stat().st_size
        except OSError as e:
            self._content_widget.update(f"파일 정보를 읽을 수 없습니다: {e}")
            return

        if file_size > MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            self._content_widget.update(
                f"파일이 너무 큽니다 ({size_mb:.1f}MB). 최대 1MB까지 표시 가능합니다."
            )
            return

        # Read raw bytes to check for binary content
        try:
            raw_bytes = path.read_bytes()
        except OSError as e:
            self._content_widget.update(f"파일을 읽을 수 없습니다: {e}")
            return

        # Detect binary file by checking for null bytes in first BINARY_CHECK_BYTES
        check_slice = raw_bytes[:BINARY_CHECK_BYTES]
        if b"\x00" in check_slice:
            self._content_widget.update(f"바이너리 파일: {path.name}")
            return

        # Decode as text with encoding error replacement
        try:
            text_content = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            try:
                text_content = raw_bytes.decode("latin-1", errors="replace")
            except Exception as e:
                self._content_widget.update(f"파일 디코딩 오류: {e}")
                return

        # Detect language and create syntax-highlighted renderable
        language = _detect_language(path)
        syntax = Syntax(
            text_content,
            language,
            theme="monokai",
            line_numbers=True,
            word_wrap=self._word_wrap,
        )

        self._content_widget.update(syntax)

        # Scroll back to top when loading a new file
        scroll = self.query_one("#viewer-scroll", VerticalScroll)
        scroll.scroll_home(animate=False)
