"""会话持久化：每个会话一个 JSON 文件，存完整的消息历史，用来续跑（--continue / --resume）。

一轮跑完、或者发生压缩时会把整份会话原子写进 JSON。一轮进行中每到一个节点边界，
把新增的消息追加到 <id>.messages.jsonl；进程被杀也能从 JSON + 日志重放到当前进度。
日志里的 append 记录带起始下标，所以 JSON 已经包含的消息不会重复。
"""

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketagent.core.compaction import SUMMARY_PREFIX
from pocketagent.interaction.attachments import message_display


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
    # 跑了多少轮：用户说一句话到 agent 停下算一轮；老会话没有这个字段，读的时候按消息估一个
    turns: int = 0
    # 任务计划（todo_write 维护的步骤清单），跟着会话落盘，界面重开时照着恢复面板
    todos: list[dict[str, Any]] = field(default_factory=list)
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

    def journal_path_for(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.messages.jsonl"

    def save(self, session: Session) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now_text()
        target = self.path_for(session.id)
        # 先写临时文件再改名，进程中途被杀也不会留下半个文件
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(session), ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(target)
        # 全量保存是权威版本：进行中日志里的消息已经进 JSON 了，删掉避免重放旧的 reset
        try:
            self.journal_path_for(session.id).unlink(missing_ok=True)
        except OSError:
            pass

    def load(self, session_id: str, recover: bool = True) -> Session:
        """recover=True 时把进行中那一轮的日志重放到消息里；只看列表/标题时传 False，省得读大文件。"""
        data = json.loads(self.path_for(session_id).read_text(encoding="utf-8"))
        session = Session(**data)
        if recover:
            self._replay_journal(session)
        if "turns" not in data:
            session.turns = _count_turns(session.messages)
        return session

    def _replay_journal(self, session: Session) -> None:
        path = self.journal_path_for(session.id)
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        messages = list(session.messages)
        for line in lines:
            record = _parse_journal_line(line)
            if record is None:
                continue
            if record["type"] == "reset":
                messages = list(record.get("messages") or [])
            elif record["type"] == "append":
                messages = _apply_append(messages, record)
        session.messages = messages

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).is_file()

    def delete(self, session_id: str) -> None:
        self.path_for(session_id).unlink(missing_ok=True)
        self.journal_path_for(session_id).unlink(missing_ok=True)

    def summaries(self) -> list[SessionSummary]:
        """所有会话的摘要，按最近更新排序。"""
        if not self.directory.is_dir():
            return []
        current: dict[str, tuple[float, SessionSummary]] = {}
        for path in self.directory.glob("*.json"):
            modified_at = path.stat().st_mtime
            cached = self._summary_cache.get(path.stem)
            if cached is None or cached[0] != modified_at:
                session = self.load(path.stem, recover=False)
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
        sessions = [self.load(path.stem, recover=False) for path in self.directory.glob("*.json")]
        if workspace is not None:
            workspace_text = str(workspace.resolve())
            sessions = [session for session in sessions if session.workspace == workspace_text]
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return sessions

    def latest(self, workspace: Path) -> Session | None:
        sessions = self.list_sessions(workspace)
        if not sessions:
            return None
        # 列表用的是 JSON 里的元数据；真正续跑时再重放进行中那一轮的日志
        return self.load(sessions[0].id)


def _count_turns(messages: list[dict[str, Any]]) -> int:
    """老会话没有 turns 字段，按消息估：每轮开始会追加一条 user 消息，跳过内部提示和压缩摘要。

    干活时的插话也是 user 消息，会被多算；只在首次读老会话时估一次，之后 turns 落盘就走计数。
    """
    count = 0
    for message in messages:
        if message.get("role") != "user" or message.get("_kind"):
            continue
        if not message_display(message)["text"].startswith(SUMMARY_PREFIX):
            count += 1
    return count


def _parse_journal_line(line: str) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        # 进程被杀时最后一行可能只写了一半，前面的记录仍然可用
        return None
    if not isinstance(record, dict) or record.get("type") not in ("append", "reset"):
        return None
    if not isinstance(record.get("messages"), list):
        return None
    return record


def _apply_append(messages: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """把 append 记录接到消息后面；JSON 里已经有的部分跳过，避免全量保存后重复。"""
    appended = list(record["messages"])
    start = record.get("start")
    if not isinstance(start, int) or start < 0:
        start = len(messages)
    already_have = len(messages) - start
    if already_have > 0:
        appended = appended[already_have:] if already_have < len(appended) else []
    messages.extend(appended)
    return messages


class SessionJournal:
    """一轮进行中的消息日志。

    每到一个节点边界调用 record：新增的消息写 append，压缩把历史换掉了就写 reset。
    一轮正常结束时 Agent 会做一次全量保存并 clear，所以平时日志只在磁盘上停留一轮。
    """

    def __init__(self, store: SessionStore, session: Session) -> None:
        self.store = store
        self.session = session
        self.journaled_count = len(session.messages)
        self.history_version = 0
        self.enabled = True

    async def record(self, messages: list[dict[str, Any]], history_version: int) -> None:
        if not self.enabled:
            return
        record = self._build_record(messages, history_version)
        if record is None:
            return
        try:
            await asyncio.to_thread(self._append, record)
        except (OSError, TypeError, ValueError):
            # 崩溃恢复只是辅助，日志写不了不能影响这一轮
            self.enabled = False
            return
        self.journaled_count = len(messages)
        self.history_version = history_version

    def _build_record(self, messages: list[dict[str, Any]], history_version: int) -> dict[str, Any] | None:
        if history_version != self.history_version or len(messages) < self.journaled_count:
            return {"type": "reset", "history_version": history_version, "messages": list(messages)}
        if len(messages) == self.journaled_count:
            return None
        return {
            "type": "append",
            "start": self.journaled_count,
            "messages": list(messages[self.journaled_count :]),
        }

    def _append(self, record: dict[str, Any]) -> None:
        path = self.store.journal_path_for(self.session.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def clear(self, history_version: int) -> None:
        """全量保存成功之后调用：日志里的内容已经进 JSON 了。"""
        try:
            self.store.journal_path_for(self.session.id).unlink(missing_ok=True)
        except OSError:
            pass
        self.journaled_count = len(self.session.messages)
        self.history_version = history_version
