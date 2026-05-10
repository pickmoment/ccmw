# ccmw 설계 문서

> Claude Code Multi-Window — TUI 기반 Claude Code 통합 개발 환경

---

## 1. 프로젝트 개요

`ccmw`는 [Textual](https://github.com/Textualize/textual) 프레임워크로 구축된 터미널 UI(TUI) 애플리케이션으로, Claude Code CLI를 멀티 탭 터미널로 실행하면서 파일 탐색·Git 관리·AI 코드 리뷰를 단일 화면에서 제공한다.

### 핵심 가치
- **인터페이스 통합**: 파일 브라우저 + 코드 뷰어 + 터미널 + Git 패널을 한 화면에서 전환 없이 사용
- **Claude Code 네이티브**: PTY 에뮬레이션을 통해 Claude 세션을 탭으로 직접 내장
- **AI 코드 리뷰**: Git diff를 Claude에 전달해 리뷰 리포트를 즉시 생성

---

## 2. 기술 스택

| 레이어 | 선택 | 이유 |
|--------|------|------|
| TUI 프레임워크 | Textual >= 0.60 | 반응형 위젯 트리, CSS 스타일링, asyncio 내장 |
| 렌더링 | Rich >= 13 | 구문 강조, 색상 텍스트, Syntax 위젯 |
| PTY 에뮬레이션 | pyte >= 0.8.2 | VT100/ANSI 스크린 버퍼 처리 |
| 패키지 관리 | uv + hatchling | src layout, 빠른 의존성 해석 |
| Python | >= 3.11 | `from __future__ import annotations`, match 문법 |
| Git 통합 | subprocess → `git` CLI | 외부 라이브러리 의존 최소화 |
| macOS IME 감지 | ctypes → CoreFoundation | TIS API는 GUI 없는 터미널에서 동작 안 함 |

---

## 3. 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────────────┐
│  CCMWApp (Textual App)                                              │
│                                                                     │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ FileBrowser │  │  FileViewer  │  │   MultiTerminalPanel      │  │
│  │  (18% 폭)  │  │  (42% 폭)   │  │      (나머지 폭)          │  │
│  │             │  │              │  │  ┌──────┬──────┬──────┐  │  │
│  │ DirectoryTree│  │ Rich Syntax  │  │  │ Tab1 │ Tab2 │ Tab3 │  │  │
│  │ + Git Status │  │  Highlight   │  │  └──────┴──────┴──────┘  │  │
│  └─────────────┘  └──────────────┘  │  ┌──────────────────────┐  │  │
│                                     │  │  TerminalPanel (PTY)  │  │  │
│  ┌──────────────────────────────────┴──┴──────────────────────┘  │  │
│  │  SessionPanel (오버레이, 토글)                                  │  │
│  │  GitPanel (오버레이, 토글)                                      │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 레이아웃 분할

```
화면 폭 100%
├── #file-browser-pane  18%  (항상 표시)
├── #file-viewer        42%  (viewer-open 클래스 시에만 표시)
└── #terminal-panel     1fr  (기본 82%, viewer 열림 시 40%)

하단 오버레이 (display: none ↔ block 토글)
├── #session-panel      최대 14행 높이
└── #git-panel          35행 고정 높이
```

---

## 4. 컴포넌트 설계

### 4.1 CCMWApp (`app.py`)

**역할**: 최상위 Textual 앱. 전역 키 바인딩 처리, 패널 간 이벤트 중계, 상태 관리.

**주요 상태**

| 필드 | 타입 | 설명 |
|------|------|------|
| `_start_cwd` | `Path` | 시작 작업 디렉터리 |
| `_sessions` | `SessionManager` | Claude 세션 로더 |
| `_session_panel_visible` | `bool` | 세션 패널 표시 여부 |
| `_git_panel_visible` | `bool` | Git 패널 표시 여부 |
| `_viewer_visible` | `bool` | 파일 뷰어 표시 여부 |
| `_last_lang` | `str` | 마지막 IME 언어 캐시 |

**주기적 타이머**

| 간격 | 동작 |
|------|------|
| 0.2초 | `_poll_input_lang()` — IME 언어 감지 및 상태바 갱신 |
| 5.0초 | `_poll_git_status()` — FileBrowser git 상태 새로고침 |

**이벤트 흐름**

```
FileBrowser.FileSelected
  └─→ FileViewer.show_file()
  └─→ _open_viewer()

FileBrowser.GitStatusUpdated
  └─→ #git-branch 위젯 업데이트 (브랜치명 + 변경 파일 수)

SessionPanel.SessionSelected
  └─→ MultiTerminalPanel.new_tab(cwd, ["claude", "--resume", session_id])

GitPanel.CloseRequested / SessionPanel.CloseRequested
  └─→ 해당 패널 숨김 처리
```

---

### 4.2 TerminalPanel (`widgets/terminal_panel.py`)

**역할**: PTY 기반 인터랙티브 터미널 위젯. `pyte`로 VT100 스크린을 에뮬레이션하고 Rich `Text`로 렌더링.

**핵심 설계 결정**

- **PTY fork**: `pty.fork()` → 자식 프로세스에서 `os.execvp()`, 부모는 마스터 fd 보유
- **비동기 I/O**: `asyncio.get_running_loop().add_reader(fd, callback)` — 이벤트 루프에 fd 등록, 블로킹 없이 읽기
- **스크린 버퍼**: `pyte.HistoryScreen(cols, rows, history=500)` — 500줄 히스토리 보존
- **렌더링**: `pyte` 셀의 fg/bg/bold/italic/underscore/reverse 속성 → `@lru_cache` 캐싱된 Rich 스타일 문자열로 변환
- **CSI 필터**: Kitty keyboard protocol 등 `pyte`가 처리 못 하는 시퀀스를 정규식으로 제거
- **윈도우 크기**: `TIOCSWINSZ` ioctl로 PTY에 터미널 크기 전달, `on_resize` 시 동기화

**스크롤 구현**

```
_scroll_offset = 0  → 라이브 뷰 (항상 최하단 추적)
_scroll_offset = N  → 히스토리 N줄 위 표시

키보드/마우스 스크롤: offset 증감
입력 발생 시: offset → 0 (라이브로 복귀)
새 출력 시: offset > 0이면 히스토리 추가량만큼 보정 (화면 고정)
```

**TerminalScrollBar**: 클릭·드래그로 오프셋 직접 제어. `JumpTo` 메시지로 `TerminalPanel`에 전달.

---

### 4.3 MultiTerminalPanel (`widgets/multi_terminal_panel.py`)

**역할**: 여러 `TerminalPanel`을 Textual `TabbedContent`로 관리.

**탭 명명 규칙**: `{tab_type} {n}  ×` (`Claude 1  ×`, `Shell 2  ×`)

**탭 닫기 전략**
- 탭이 2개 이상: `TabbedContent.remove_pane()` 호출
- 탭이 1개: 세션만 종료 (`TerminalPanel.reset()`)하고 위젯은 유지 (UI 상 빈 탭 유지)

---

### 4.4 FileBrowser (`widgets/file_browser.py`)

**역할**: `DirectoryTree`를 상속해 숨김 파일 토글 + 인라인 Git 상태 표시 추가.

**Git 상태 갱신**

```
refresh_git_status()
  └─→ run_worker(thread=True, exclusive=True, group="git")
        └─→ _fetch_git_status(cwd)
              ├─→ git rev-parse --show-toplevel
              ├─→ git rev-parse --abbrev-ref HEAD
              └─→ git status --porcelain=v1 -z
        └─→ on_worker_state_changed → post_message(GitStatusUpdated)
```

**디렉터리 롤업**: 파일 상태를 상위 디렉터리까지 전파해 폴더 아이콘에도 Git 상태 표시. 우선순위: `D > U > M > R > C > A > ?`

**렌더링**: `render_label()` 오버라이드 — `[상태코드] 파일명` 형식으로 색상 접두사 삽입.

---

### 4.5 FileViewer (`widgets/file_viewer.py`)

**역할**: 선택한 파일을 구문 강조와 함께 표시하는 읽기 전용 패널.

**파일 로드 가드**

| 조건 | 처리 |
|------|------|
| 파일 크기 > 1MB | 에러 메시지 표시 |
| 바이너리 (첫 1024바이트에 null) | "바이너리 파일" 메시지 |
| 인코딩 실패 | UTF-8 → Latin-1 순으로 `errors='replace'` |

**언어 감지**: 파일 확장자 매핑 테이블 (50+ 언어), `Dockerfile`/`Containerfile`은 이름으로 감지.

**자동줄바꿈 토글**: `w` 키 → CSS 클래스 교체 + `Syntax` 재생성.

---

### 4.6 GitPanel (`widgets/git_panel.py`)

**역할**: Git 변경사항 관리, 원격 동기화, 브랜치 관리, 저장소 초기화를 단일 패널에서 제공.

#### 4.6.1 레이아웃

```
GitPanel (35행 고정)
├── #git-panel-title (1행) — 모드·원격 상태 표시
└── #git-panel-body (Horizontal)
    ├── #git-panel-left (36열 고정)
    │   ├── #changes-view (변경사항 뷰, 기본 표시)
    │   │   ├── #git-file-list (ListView, 1fr)
    │   │   ├── #git-commit-msg (Input)
    │   │   ├── #git-stage-row  [스테이지 (a)] [커밋]
    │   │   ├── #git-remote-row [Pull ↓ (p)] [Push ↑]
    │   │   ├── #btn-review     [AI 리뷰 (v)]
    │   │   └── #btn-git-init   [git init (i)]  ← 저장소 없을 때만 표시
    │   └── #branches-view (브랜치 뷰, 기본 숨김)
    │       ├── #branch-list (ListView, 1fr)
    │       ├── #branch-name-input (Input)
    │       └── #git-branch-buttons [체크아웃] [생성] [삭제]
    └── #git-diff-pane (1fr)
        ├── unified 모드: RichLog
        └── split 모드: SyncedRichLog(left) + SyncedRichLog(right)
```

#### 4.6.2 데이터 모델

```python
@dataclass
class _Change:
    xy: str       # git porcelain XY 코드 (예: "M ", " M", "??")
    path: str     # 저장소 루트 기준 상대 경로
    staged: bool  # X 컬럼(인덱스)에 변경이 있으면 True

@dataclass
class _Branch:
    name: str        # 브랜치 이름 (원격은 "origin/main" 형태)
    is_current: bool # 현재 체크아웃된 브랜치
    is_remote: bool  # 원격 추적 브랜치 여부
```

#### 4.6.3 GitPanel 상태

| 필드 | 타입 | 설명 |
|------|------|------|
| `_cwd` | `Path` | 현재 작업 디렉터리 |
| `_root` | `Path \| None` | git 저장소 루트 (`None` = 저장소 없음) |
| `_changes` | `list[_Change]` | 변경 파일 목록 |
| `_diff_mode` | `str` | `"unified"` \| `"split"` |
| `_ahead` | `int` | 원격보다 앞선 커밋 수 |
| `_behind` | `int` | 원격보다 뒤처진 커밋 수 |
| `_has_upstream` | `bool` | upstream 브랜치 설정 여부 |
| `_branch_view` | `bool` | 브랜치 뷰 활성화 여부 |
| `_branches` | `list[_Branch]` | 로컬 + 원격 브랜치 목록 |

#### 4.6.4 키 바인딩

| 키 | 동작 | 뷰 |
|----|------|-----|
| `r` | 상태 새로고침 | 공통 |
| `space` | 선택 파일 스테이지/언스테이지 | 변경사항 |
| `a` | 모든 파일 스테이지 | 변경사항 |
| `d` | Diff 모드 전환 (Unified ↔ Split) | 변경사항 |
| `p` | Pull | 변경사항 |
| `v` | AI 코드 리뷰 | 변경사항 |
| `b` | 변경사항 ↔ 브랜치 뷰 전환 | 공통 |
| `n` | 새 브랜치 이름 입력창 포커스 | 브랜치 |
| `Enter` | 선택 브랜치 체크아웃 | 브랜치 |
| `Ctrl+D` | 선택 로컬 브랜치 삭제 | 브랜치 |
| `i` | git init (저장소 없을 때만 동작) | 공통 |
| `Esc` | 패널 닫기 | 공통 |

브랜치 뷰 활성 중에는 `space`, `a`, `d`, `v`가 무시된다 (가드 처리).

#### 4.6.5 Git 헬퍼 함수

| 함수 | 명령 | 반환 |
|------|------|------|
| `_get_git_root(cwd)` | `git rev-parse --show-toplevel` | `Path \| None` |
| `_get_status(cwd)` | `git status --porcelain` | `list[_Change]` |
| `_get_remote_status(cwd)` | `git rev-list --count --left-right HEAD...@{u}` | `(ahead, behind, has_upstream)` |
| `_list_branches(cwd)` | `git branch --format=...` + `git branch -r` | `list[_Branch]` |

#### 4.6.6 워커 목록

| 워커 | 명령 | 비고 |
|------|------|------|
| `_refresh_remote_status()` | `_get_remote_status()` | `action_refresh` 호출마다 실행 |
| `_load_branches()` | `_list_branches()` | 브랜치 뷰 진입·`action_refresh` 시 |
| `_load_diff(change)` | `git diff [--cached]` | 파일 목록 하이라이트 변경마다 |
| `_do_pull()` | `git pull` | 60초 타임아웃 |
| `_do_push()` | `git push [--set-upstream origin <branch>]` | upstream 없으면 자동 설정 |
| `_do_checkout(name, is_remote)` | `git checkout [-b --track]` | 원격 브랜치는 로컬 추적 브랜치 생성 |
| `_do_create_branch(name)` | `git checkout -b <name>` | 생성 후 즉시 전환 |
| `_do_delete_branch(name)` | `git branch -d <name>` | 안전 삭제 (merge 안 된 브랜치 보호) |
| `_do_init()` | `git init` | 완료 후 `_root` 재탐지 및 전체 새로고침 |

모든 워커는 `@work(thread=True)`로 실행되며, UI 업데이트는 `app.call_from_thread()`를 통해 메인 스레드에서 처리한다.

#### 4.6.7 원격 상태 표시

```
타이틀 예시: git  변경 3  스테이징됨 1  ⇡2 ⇣0
```

- `⇡N`: 로컬이 원격보다 N커밋 앞섬 (push 필요)
- `⇣N`: 원격이 로컬보다 N커밋 앞섬 (pull 필요)
- upstream 없으면 표시 없음; Push 시 자동으로 `--set-upstream origin <branch>` 적용

#### 4.6.8 뷰 전환 메커니즘

```
b 키
  ├── 변경사항 뷰 → 브랜치 뷰
  │     #changes-view.add_class("hidden")
  │     #branches-view.remove_class("hidden")
  │     _load_branches()  (worker)
  └── 브랜치 뷰 → 변경사항 뷰
        #branches-view.add_class("hidden")
        #changes-view.remove_class("hidden")
```

#### 4.6.9 git init 흐름

```
저장소 없음 (_root is None)
  → #btn-git-init 버튼 표시
  → 파일 목록: "(Git 저장소 없음  —  i 키로 git init)"

i 키 또는 버튼 클릭
  → _do_init() worker
      git init (cwd)
      성공 → _root = _get_git_root() 재탐지
           → action_refresh() 호출 (버튼 숨김, 목록 갱신)
      실패 → 에러 토스트
```

#### 4.6.10 SyncedRichLog

`RichLog`를 상속. `watch_scroll_y` / `watch_scroll_x` 반응형 속성으로 split 뷰의 좌우 패널 스크롤을 항상 동기화한다.

---

### 4.7 ClaudeReviewModal (`widgets/claude_review_modal.py`)

**역할**: Git diff를 Claude에 전달해 AI 리뷰를 받고, 이어서 추가 질문을 가능하게 하는 모달.

**Claude 호출 방식**

```
claude -p --output-format stream-json --verbose [--resume <session_id>] "<prompt>"
```

**스트림 처리**

```
stdout 줄 단위 읽기 (proc.stdout 이터레이션)
  ├─→ type=system/subtype=init    → session_id 저장
  ├─→ type=content_block_delta   → text_delta 버퍼에 누적
  ├─→ type=assistant             → content 블록 처리
  └─→ type=result                → session_id 갱신, 버퍼 flush
```

**세션 지속**: `_session_id`를 저장해 추가 질문 시 `--resume`으로 대화 이어받기.

**줄 버퍼링**: `\n` 기준으로 분할해 완성된 줄만 `RichLog.write()` — 스트리밍 출력 시 깨짐 방지.

**클립보드**: `pbcopy` (macOS) → `xclip -selection clipboard` (Linux) 순으로 폴백.

---

### 4.8 SessionManager (`session_manager.py`)

**역할**: `~/.claude/` 디렉터리에서 Claude 세션 정보를 읽어 목록 제공.

**데이터 소스 병합**

| 소스 | 경로 | 제공 정보 |
|------|------|-----------|
| Live sessions | `~/.claude/sessions/*.json` | PID, 상태(busy/idle/stopped), 시작 시각 |
| Historical | `~/.claude/projects/<encoded_cwd>/*.jsonl` | AI 제목, 마지막 프롬프트, 첫 메시지 시각 |

**경로 인코딩**: `/path/to/project` → `-path-to-project` (슬래시를 하이픈으로 치환)

**프로세스 생존 확인**: `os.kill(pid, 0)` — 시그널 0은 kill 없이 프로세스 존재만 확인.

---

### 4.9 InputSource (`input_source.py`)

**역할**: macOS에서 현재 IME 언어를 감지해 한/영 구분.

**구현 이유**: TIS API(`TISCopyCurrentKeyboardInputSource`)는 GUI 이벤트 큐 없이 동작하는 터미널 프로세스에서 올바른 결과를 반환하지 않음. 대신 `CFPreferences`로 `com.apple.HIToolbox.AppleSelectedInputSources`를 직접 읽어 `"Korean"` 문자열 포함 여부를 확인.

---

## 5. 이벤트 메시지 목록

| 메시지 클래스 | 발신 위젯 | 수신 처리 |
|---------------|-----------|-----------|
| `FileBrowser.GitStatusUpdated` | `FileBrowser` | `CCMWApp` → `#git-branch` 업데이트 |
| `FileBrowser.FileSelected` (DirectoryTree) | `FileBrowser` | `CCMWApp` → `FileViewer.show_file()` |
| `FileViewer.CloseRequested` | `FileViewer` | `CCMWApp` → `_close_viewer()` |
| `SessionPanel.SessionSelected` | `SessionPanel` | `CCMWApp` → 새 탭에서 `--resume` |
| `SessionPanel.CloseRequested` | `SessionPanel` | `CCMWApp` → 패널 숨김 |
| `GitPanel.CloseRequested` | `GitPanel` | `CCMWApp` → 패널 숨김 |
| `TerminalScrollBar.JumpTo` | `TerminalScrollBar` | `TerminalPanel` → `_scroll_offset` 설정 |

---

## 6. 키 바인딩 설계 원칙

- **전역 바인딩**: `CCMWApp.BINDINGS` — 어느 포커스에서도 동작
- **패널 전용 바인딩**: 각 패널 `BINDINGS` (`priority=True`) — 패널 포커스 시 전역보다 우선
- **충돌 해결**: `r`은 전역(새로고침)과 GitPanel(새로고침) 모두 정의되지만 GitPanel 포커스 시 `priority=True`로 패널이 먼저 처리

---

## 7. 비동기 실행 모델

```
asyncio 이벤트 루프 (Textual 내장)
│
├─ PTY fd read callback (add_reader)  ← 블로킹 없이 PTY 출력 수신
├─ Timer 0.2s  ← IME 언어 폴링
├─ Timer 5.0s  ← Git 상태 폴링 (FileBrowser)
│
└─ @work(thread=True) 워커 스레드 풀
   ├─ FileBrowser._fetch_git_status()      (git status, branch)
   ├─ GitPanel._refresh_remote_status()    (git rev-list ahead/behind)
   ├─ GitPanel._load_diff()                (git diff)
   ├─ GitPanel._load_branches()            (git branch -a)
   ├─ GitPanel._do_pull()                  (git pull, 60s timeout)
   ├─ GitPanel._do_push()                  (git push [--set-upstream])
   ├─ GitPanel._do_checkout()              (git checkout [-b --track])
   ├─ GitPanel._do_create_branch()         (git checkout -b)
   ├─ GitPanel._do_delete_branch()         (git branch -d)
   ├─ GitPanel._do_init()                  (git init)
   ├─ ClaudeReviewModal._start_review()    (claude -p 스트리밍)
   └─ ClaudeReviewModal._start_follow_up() (claude -p --resume)
       └─ app.call_from_thread() → 메인 스레드 UI 업데이트
```

---

## 8. CSS 레이아웃 구조 (`styles.tcss`)

Textual CSS의 주요 레이아웃 패턴:

```css
/* 기본 상태: viewer 없음 */
#file-viewer { display: none; }

/* e 키 → .viewer-open 클래스 토글 */
#main-container.viewer-open #file-viewer { display: block; width: 42%; }
#main-container.viewer-open #terminal-panel { width: 40%; }

/* 패널 오버레이 토글 */
.hidden { display: none; }   /* 클래스 추가/제거로 패널 ON/OFF */
```

포커스 하이라이트: `:focus-within` 셀렉터로 활성 패널에 accent 색상 테두리 적용.

---

## 9. 패키지 구조

```
src/ccmw/
├── __init__.py          패키지 초기화
├── __main__.py          python -m ccmw 진입점
├── app.py               CCMWApp + NewTabPicker + main()
├── input_source.py      macOS IME 언어 감지
├── session_manager.py   Claude 세션 파일 파서
├── styles.tcss          Textual CSS (다크 테마)
└── widgets/
    ├── __init__.py
    ├── file_browser.py        FileBrowser (DirectoryTree 확장)
    ├── file_viewer.py         FileViewer (Syntax 강조)
    ├── git_panel.py           GitPanel + SyncedRichLog
    ├── claude_review_modal.py ClaudeReviewModal (스트리밍 AI 리뷰)
    ├── multi_terminal_panel.py MultiTerminalPanel (탭 관리)
    ├── terminal_panel.py      TerminalPanel (PTY 에뮬레이터)
    └── session_panel.py       SessionPanel (세션 목록)
```

---

## 10. 의존성 및 외부 요구사항

| 의존성 | 용도 | 필수 여부 |
|--------|------|-----------|
| `textual >= 0.60` | TUI 프레임워크 | 필수 |
| `rich >= 13` | 렌더링, Syntax | 필수 (textual 내장) |
| `pyte >= 0.8.2` | PTY 스크린 에뮬레이션 | 필수 |
| `claude` CLI | 터미널 세션, AI 리뷰 | 런타임 필수 |
| `git` CLI | Git 상태, diff, 커밋, push | Git 기능에 필수 |
| `pbcopy` / `xclip` | 클립보드 복사 | 선택 (OS 제공) |

---

## 11. 개선 가능 영역

| 영역 | 현재 한계 | 개선 방향 |
|------|-----------|-----------|
| 클립보드 | macOS/Linux만 지원 | `pyperclip` 또는 Textual 내장 clipboard API 검토 |
| PTY 렌더링 | 마우스 선택 불가 | Textual `SelectionList` 또는 별도 copy 모드 구현 |
| 브랜치 삭제 | `-d` 안전 삭제만 지원 | `-D` 강제 삭제 확인 다이얼로그 추가 |
| 원격 브랜치 삭제 | 미지원 | `git push origin --delete <branch>` 추가 |
| git stash | 미지원 | stash list·pop·drop 기능 추가 |
| Merge conflict | 미지원 | conflict 파일 표시 및 에디터 연동 |
| 세션 필터 | 현재 cwd만 표시 | 전체 세션 표시 옵션 추가 |
| IME 감지 | macOS 전용 | Linux(IBus/fcitx) 지원 확장 가능 |
| 설정 | 하드코딩 | `~/.config/ccmw/config.toml` 도입 |
