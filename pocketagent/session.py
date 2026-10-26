"""会话持久化：每个会话一个 JSON 文件，存完整的消息历史，用来续跑（--continue / --resume）。"""

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketagent.attachments import message_display
from pocketagent.compaction import SUMMARY_PREFIX


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Session:
    id: str
    workspace: str
    # 临时任务：workspace 是自动建的临时目录（~/.pocketagent/scratch 下），不是用户的某个项目
    ephemeral: bool = False
    created_at: str = field(default_factory=_now_text)
    updated_at: str = field(default_factory=_now_text)
    # 用的是配置文件里哪个模型（id），续跑时接着用它
    model_profile: str | None = None
    # 审批模式，续跑时接着用
    approval_mode: str | None = None
    # 用户改过的标题；没改就用第一句话
    title: str | None = None
    # 谁建的：空是用户，"automation:<id>" 是定时任务
    source: str | None = None
    # 影子 git 里的检查点（见 checkpoints.py），第一个是任务开始时的样子
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def first_user_text(self) -> str:
        """第一句用户的话；压缩后放在开头的摘要不算。"""
        for message in self.messages:
            if message.get("role") != "user" or message.get("_kind"):
                continue
            text = message_display(message)["text"]
            if not text.startswith(SUMMARY_PREFIX):
                return text
        return ""

    def display_title(self) -> str:
        if self.title:
            return self.title
        first_text = self.first_user_text().strip()
        return first_text.splitlines()[0][:80] if first_text else "新任务"


@dataclass
class SessionSummary:
    """侧栏任务列表要的那几项，不含消息。"""

    id: str
    workspace: str
    title: str
    updated_at: str
    source: str | None
    ephemeral: bool = False


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        # 会话 id → (文件修改时间, 摘要)。任务列表每隔几秒刷新一次，只重读改过的文件
        self._summary_cache: dict[str, tuple[float, SessionSummary]] = {}

    def new_session(self, workspace: Path, ephemeral: bool = False) -> Session:
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        return Session(id=session_id, workspace=str(workspace.resolve()), ephemeral=ephemeral)

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

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).is_file()

    def delete(self, session_id: str) -> None:
        self.path_for(session_id).unlink(missing_ok=True)

    def summaries(self) -> list[SessionSummary]:
        """所有会话的摘要，按最近更新排序。"""
        if not self.directory.is_dir():
            return []
        current: dict[str, tuple[float, SessionSummary]] = {}
        for path in self.directory.glob("*.json"):
            modified_at = path.stat().st_mtime
            cached = self._summary_cache.get(path.stem)
            if cached is None or cached[0] != modified_at:
                session = self.load(path.stem)
                summary = SessionSummary(
                    id=session.id,
                    workspace=session.workspace,
                    title=session.display_title(),
                    updated_at=session.updated_at,
                    source=session.source,
                    ephemeral=session.ephemeral,
                )
                cached = (modified_at, summary)
            current[path.stem] = cached
        self._summary_cache = current
        summaries = [summary for _, summary in current.values()]
        summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
        return summaries

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
