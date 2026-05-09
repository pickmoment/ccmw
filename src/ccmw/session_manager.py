"""SessionManager — reads Claude Code session data from ~/.claude/."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Session:
    session_id: str
    cwd: str
    timestamp: str          # ISO from first user message
    ai_title: str = ""
    last_prompt: str = ""
    status: str = "stopped"  # idle / busy / stopped
    pid: int = 0
    updated_at: int = 0     # epoch ms


def _encode_path(p: Path) -> str:
    """Convert absolute path to Claude project dir name: /a/b → -a-b."""
    return str(p).replace("/", "-")


class SessionManager:
    def __init__(self, project_cwd: Path) -> None:
        self._cwd = project_cwd.resolve()
        self._sessions_dir = Path.home() / ".claude" / "sessions"
        self._project_dir = Path.home() / ".claude" / "projects" / _encode_path(self._cwd)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list(self) -> list[Session]:
        live = self._load_live_sessions()
        historical = self._load_historical_sessions(live)
        merged = {s.session_id: s for s in historical}
        for s in live.values():
            if s.session_id in merged:
                merged[s.session_id].status = s.status
                merged[s.session_id].pid = s.pid
                merged[s.session_id].updated_at = s.updated_at
            else:
                merged[s.session_id] = s
        result = sorted(merged.values(), key=lambda s: s.timestamp, reverse=True)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_live_sessions(self) -> dict[str, Session]:
        live: dict[str, Session] = {}
        if not self._sessions_dir.exists():
            return live
        for f in self._sessions_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("cwd") != str(self._cwd):
                    continue
                sid = data["sessionId"]
                pid = data.get("pid", 0)
                status = data.get("status", "idle")
                if pid and not _is_alive(pid):
                    status = "stopped"
                live[sid] = Session(
                    session_id=sid,
                    cwd=data.get("cwd", ""),
                    timestamp=_ms_to_iso(data.get("startedAt", 0)),
                    status=status,
                    pid=pid,
                    updated_at=data.get("updatedAt", 0),
                )
            except Exception:
                pass
        return live

    def _load_historical_sessions(self, live: dict[str, Session]) -> list[Session]:
        sessions: list[Session] = []
        if not self._project_dir.exists():
            return sessions
        for f in self._project_dir.glob("*.jsonl"):
            sid = f.stem
            s = Session(session_id=sid, cwd=str(self._cwd), timestamp="")
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    try:
                        d = json.loads(line)
                        t = d.get("type")
                        if t == "user" and not s.timestamp:
                            s.timestamp = d.get("timestamp", "")
                        elif t == "ai-title":
                            s.ai_title = d.get("aiTitle", "")
                        elif t == "last-prompt":
                            s.last_prompt = str(d.get("lastPrompt", ""))[:80]
                    except Exception:
                        pass
            except Exception:
                pass
            if not s.timestamp:
                mtime = f.stat().st_mtime
                s.timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            sessions.append(s)
        return sessions


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ms_to_iso(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
