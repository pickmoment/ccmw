from pathlib import Path

from rich.console import Group
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from PIL import Image

MAX_FILE_SIZE = 1024 * 1024        # 1MB  (text files)
MAX_IMAGE_FILE_SIZE = 10 * 1024 * 1024  # 10MB (image files)
BINARY_CHECK_BYTES = 1024

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".webp", ".ico", ".tiff", ".tif", ".avif",
}

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
    suffix = path.suffix.lower()
    if path.name.lower() in ("dockerfile", "containerfile"):
        return "dockerfile"
    return EXTENSION_TO_LANGUAGE.get(suffix, "text")


def _format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


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

    def _render_image(self, path: Path) -> None:
        """Render an image file using half-block Unicode characters."""
        if self._content_widget is None:
            return

        file_size = path.stat().st_size
        human_size = _format_file_size(file_size)

        try:
            with Image.open(path) as img:
                orig_format = img.format or path.suffix.lstrip(".").upper()
                orig_mode = img.mode
                orig_w, orig_h = img.size

                meta = Text()
                meta.append(f"{path.name}\n", style="bold")
                meta.append(
                    f"크기: {orig_w}×{orig_h}px  파일: {human_size}"
                    f"  포맷: {orig_format}  모드: {orig_mode}\n\n",
                    style="dim",
                )

                # Determine render width from widget size (fallback 80, cap 120)
                widget_w = self.size.width
                max_width = max(20, min(widget_w if widget_w > 0 else 80, 120))

                target_w = min(max_width, orig_w)
                target_h = round(orig_h * (target_w / orig_w))
                # half-block pairs 2 pixel rows per line; ensure even height
                if target_h % 2 != 0:
                    target_h += 1
                if target_h == 0:
                    target_h = 2

                rgb_img = img.convert("RGBA")
                rgb_img = rgb_img.resize((target_w, target_h), Image.LANCZOS)
                pixels = list(rgb_img.getdata())

                image_text = Text()
                for row in range(0, target_h, 2):
                    for col in range(target_w):
                        top = pixels[row * target_w + col]        # RGBA
                        if row + 1 < target_h:
                            bottom = pixels[(row + 1) * target_w + col]
                        else:
                            bottom = (0, 0, 0, 255)

                        tr, tg, tb, ta = top
                        br, bg, bb, ba = bottom

                        fg = f"#{br:02x}{bg:02x}{bb:02x}" if ba > 0 else "default"
                        bg_color = f"#{tr:02x}{tg:02x}{tb:02x}" if ta > 0 else "default"
                        image_text.append("▄", style=Style(color=fg, bgcolor=bg_color))
                    image_text.append("\n")

                self._content_widget.update(Group(meta, image_text))

        except Exception as e:
            self._content_widget.update(
                f"[bold]{path.name}[/bold]\n"
                f"파일 크기: {human_size}\n\n"
                f"[red]이미지를 열 수 없습니다: {e}[/red]"
            )

    def show_file(self, path: Path) -> None:
        """Display the contents of the given file path."""
        self.current_path = path

        if self._content_widget is None:
            self._content_widget = self.query_one("#file-content", Static)

        self._update_title()

        if not path.exists():
            self._content_widget.update(f"파일을 찾을 수 없습니다: {path}")
            return

        if not path.is_file():
            self._content_widget.update(f"파일이 아닙니다: {path}")
            return

        try:
            file_size = path.stat().st_size
        except OSError as e:
            self._content_widget.update(f"파일 정보를 읽을 수 없습니다: {e}")
            return

        # Handle image files before binary detection
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            if file_size > MAX_IMAGE_FILE_SIZE:
                size_mb = file_size / (1024 * 1024)
                self._content_widget.update(
                    f"파일이 너무 큽니다 ({size_mb:.1f}MB). 이미지는 최대 10MB까지 표시 가능합니다."
                )
                return
            self._render_image(path)
            scroll = self.query_one("#viewer-scroll", VerticalScroll)
            scroll.scroll_home(animate=False)
            return

        if file_size > MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            self._content_widget.update(
                f"파일이 너무 큽니다 ({size_mb:.1f}MB). 최대 1MB까지 표시 가능합니다."
            )
            return

        try:
            raw_bytes = path.read_bytes()
        except OSError as e:
            self._content_widget.update(f"파일을 읽을 수 없습니다: {e}")
            return

        check_slice = raw_bytes[:BINARY_CHECK_BYTES]
        if b"\x00" in check_slice:
            self._content_widget.update(f"바이너리 파일: {path.name}")
            return

        try:
            text_content = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            try:
                text_content = raw_bytes.decode("latin-1", errors="replace")
            except Exception as e:
                self._content_widget.update(f"파일 디코딩 오류: {e}")
                return

        language = _detect_language(path)
        syntax = Syntax(
            text_content,
            language,
            theme="monokai",
            line_numbers=True,
            word_wrap=self._word_wrap,
        )

        self._content_widget.update(syntax)

        scroll = self.query_one("#viewer-scroll", VerticalScroll)
        scroll.scroll_home(animate=False)
