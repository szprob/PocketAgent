"""会话持久化：每个会话一个 JSON 文件，存完整的消息历史，用来续跑（--continue / --resume）。"""

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Session:
    id: str
    workspace: str
    created_at: str = field(default_factory=_now_text)
    updated_at: str = field(default_factory=_now_text)
    # 用的是配置文件里哪个模型（id），续跑时接着用它
    model_profile: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    def first_user_text(self) -> str:
        for message in self.messages:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return message["content"]
        return ""


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def new_session(self, workspace: Path) -> Session:
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        return Session(id=session_id, workspace=str(workspace.resolve()))

    def path_for(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def save(self, session: Session) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now_text()
        target = self.path_for(session.id)
        # 先写临时文件再改名，进程中途被杀也不会留下半个文件
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(session), ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(target)

    def load(self, session_id: str) -> Session:
        data = json.loads(self.path_for(session_id).read_text(encoding="utf-8"))
        return Session(**data)

    def list_sessions(self, workspace: Path | None = None) -> list[Session]:
        """按最近更新排序；给了 workspace 就只要这个目录下的会话。"""
        if not self.directory.is_dir():
            return []
        sessions = [self.load(path.stem) for path in self.directory.glob("*.json")]
        if workspace is not None:
            workspace_text = str(workspace.resolve())
            sessions = [session for session in sessions if session.workspace == workspace_text]
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return sessions

    def latest(self, workspace: Path) -> Session | None:
        sessions = self.list_sessions(workspace)
        return sessions[0] if sessions else None
