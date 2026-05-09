# ccmw — Claude Code Multi-Window

터미널에서 Claude Code를 멀티 탭으로 실행하고, 파일 브라우저·Git 패널·AI 코드 리뷰를 한 화면에서 사용할 수 있는 TUI 도구입니다.

[Textual](https://github.com/Textualize/textual) 기반으로 작성되었으며 `claude` CLI가 설치된 환경에서 동작합니다.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| **멀티 터미널** | Claude 세션과 Shell을 탭으로 나란히 실행 |
| **파일 브라우저** | 디렉터리 탐색 및 Git 상태 인라인 표시 |
| **파일 뷰어** | 선택한 파일을 우측 패널에서 즉시 확인 |
| **Git 패널** | 변경 파일 목록, 스테이지/언스테이지, 커밋, Push |
| **Diff 뷰어** | Unified / Split 두 가지 모드 지원 |
| **AI 코드 리뷰** | 변경 diff를 Claude에게 전달해 리뷰 리포트 생성 |
| **세션 관리** | 기존 Claude 세션 목록 조회 및 재개 |
| **입력 언어 표시** | 현재 IME 언어(한/EN)를 상태바에 표시 |

---

## 설치

Python 3.11 이상 및 [Claude Code CLI](https://github.com/anthropics/claude-code) 설치가 필요합니다.

```bash
# uv 사용 (권장)
uv pip install .

# 또는 pip
pip install .
```

---

## 실행

```bash
# 현재 디렉터리에서 실행
ccmw

# 작업 디렉터리 지정
ccmw --cwd /path/to/project

# 버전 확인
ccmw --version
```

---

## 키 바인딩

### 전역

| 키 | 동작 |
|----|------|
| `q` | 종료 |
| `Tab` / `Shift+Tab` | 패널 포커스 이동 |
| `Ctrl+N` | 새 탭 열기 (Claude / Shell 선택) |
| `Ctrl+W` | 활성 탭 닫기 |
| `h` | 숨김 파일 토글 |
| `e` | 파일 뷰어 토글 |
| `s` | 세션 목록 열기/닫기 |
| `r` | 세션 목록 새로고침 |
| `g` | Git 패널 열기/닫기 |

### Git 패널

| 키 | 동작 |
|----|------|
| `r` | 상태 새로고침 |
| `Space` | 선택 파일 스테이지/언스테이지 |
| `a` | 모든 파일 스테이지 |
| `d` | Diff 모드 전환 (Unified ↔ Split) |
| `v` | AI 코드 리뷰 시작 |
| `ESC` | 패널 닫기 |

### AI 리뷰 모달

| 키 | 동작 |
|----|------|
| `c` | 리뷰 내용 클립보드 복사 |
| `ESC` | 닫기 |
| `Enter` | 추가 질문 전송 |

---

## 의존성

```toml
python = ">=3.11"
textual = ">=0.60"
rich = ">=13"
pyte = ">=0.8.2"
```

---

## 라이선스

MIT
