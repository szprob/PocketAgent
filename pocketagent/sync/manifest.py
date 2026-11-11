"""清单：~/.pocketagent 下哪些文件参与同步。

默认只传会话和项目记忆：这是换台机器后真正想接着用的东西，加起来通常十几 MB。
config / automations / mcp / hooks 要显式打开——它们带密钥、带绝对路径，或者会在另一台机器上
自己跑起来（自动化尤其容易变成两边同时触发）。
不传：shadow/（影子 git 检查点，和本机工作区状态绑死）、traces/、tool_outputs/、attachments/、
usage.jsonl、sync/ 自己。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

# 会话 JSON 里的这几个字段指向本机的影子 git 和 worktree 目录，换机器后是无效路径。
# 上传时抹掉，下载时再保留本地那份（见 restore_machine_fields）。
MACHINE_FIELDS = ("checkpoints", "worktree", "worktree_fallback")

KIND_SESSION = "session"
KIND_PROJECT_MEMORY = "project-memory"
KIND_CONFIG = "config"
KIND_AUTOMATIONS = "automations"
KIND_MCP = "mcp"
KIND_HOOK = "hook"


@dataclass
class ManifestEntry:
    relative_path: str
    path: Path
    size: int
    kind: str


def collect_entries(home_dir: Path, includes: list[str], max_file_bytes: int) -> tuple[list[ManifestEntry], list[str]]:
    """返回 (要同步的文件, 跳过的文件说明)。路径统一用 / 分隔，排序稳定方便对比。"""
    entries: list[ManifestEntry] = []
    skipped: list[str] = []

    if "sessions" in includes:
        sessions = home_dir / "sessions"
        if sessions.is_dir():
            for path in sorted(sessions.glob("*.json")):
                if _is_ephemeral_session(path):
                    # 临时任务的工作区是 ~/.pocketagent/scratch 下的随机目录，换台机器没有意义
                    continue
                entries.append(_make_entry(home_dir, path, KIND_SESSION, max_file_bytes, skipped))
            # 进行中的 .messages.jsonl 不传：那是半轮的临时日志，JSON 才是权威版本；
            # 传过去反而可能在没有对应上下文的机器上重放出半截对话。

    if "projects" in includes:
        projects = home_dir / "projects"
        if projects.is_dir():
            for path in sorted(projects.rglob("*.md")):
                entries.append(_make_entry(home_dir, path, KIND_PROJECT_MEMORY, max_file_bytes, skipped))

    single_files = {
        "config": ("config.json", KIND_CONFIG),
        "automations": ("automations.json", KIND_AUTOMATIONS),
        "mcp": ("mcp.json", KIND_MCP),
    }
    for include, (name, kind) in single_files.items():
        if include not in includes:
            continue
        path = home_dir / name
        if path.is_file():
            entries.append(_make_entry(home_dir, path, kind, max_file_bytes, skipped))

    if "hooks" in includes:
        hooks = home_dir / "hooks"
        if hooks.is_dir():
            for path in sorted(hooks.rglob("*")):
                if path.is_file() and not path.name.endswith(".tmp"):
                    entries.append(_make_entry(home_dir, path, KIND_HOOK, max_file_bytes, skipped))

    entries = [entry for entry in entries if entry is not None]
    entries.sort(key=lambda entry: entry.relative_path)
    return entries, skipped


def _make_entry(
    home_dir: Path, path: Path, kind: str, max_file_bytes: int, skipped: list[str]
) -> ManifestEntry | None:
    size = path.stat().st_size
    relative_path = path.relative_to(home_dir).as_posix()
    if size > max_file_bytes:
        skipped.append(f"{relative_path}（{_human_size(size)}，超过单文件上限）")
        return None
    return ManifestEntry(relative_path=relative_path, path=path, size=size, kind=kind)


def _is_ephemeral_session(path: Path) -> bool:
    """读一下会话头，判断是不是临时任务。读不出来就当普通会话，照常同步。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(data, dict) and bool(data.get("ephemeral"))


def path_included(relative_path: str, includes: list[str]) -> bool:
    """关掉某一类同步时，远端已有的那类文件既不下也不删，原样放着。"""
    if relative_path.startswith("sessions/"):
        return "sessions" in includes
    if relative_path.startswith("projects/"):
        return "projects" in includes
    if relative_path == "config.json":
        return "config" in includes
    if relative_path == "automations.json":
        return "automations" in includes
    if relative_path == "mcp.json":
        return "mcp" in includes
    if relative_path.startswith("hooks/"):
        return "hooks" in includes
    return False


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare_upload(entry: ManifestEntry, data: bytes) -> bytes:
    """上传前把只在本机有意义的字段抹掉。"""
    if entry.kind == KIND_SESSION:
        return strip_machine_fields(data)
    if entry.kind == KIND_CONFIG:
        return strip_web_token(data)
    return data


def prepare_upload_for_path(relative_path: str, data: bytes) -> bytes:
    """合并结果没有 ManifestEntry 时按路径判断类型。"""
    if relative_path.startswith("sessions/"):
        return strip_machine_fields(data)
    if relative_path == "config.json":
        return strip_web_token(data)
    return data


def strip_machine_fields(data: bytes) -> bytes:
    session = _load_json_object(data)
    if session is None:
        return data
    changed = False
    for name in MACHINE_FIELDS:
        if session.get(name):
            session[name] = None
            changed = True
    if not changed:
        return data
    return _dump_json(session)


def restore_machine_fields(remote_data: bytes, local_data: bytes | None) -> bytes:
    """远端会话是没有检查点的；本机如果还有同一份会话，把本机字段接回去，界面上的「撤销」才还能用。"""
    if local_data is None:
        return remote_data
    remote_session = _load_json_object(remote_data)
    local_session = _load_json_object(local_data)
    if remote_session is None or local_session is None:
        return remote_data
    changed = False
    for name in MACHINE_FIELDS:
        if local_session.get(name) and not remote_session.get(name):
            remote_session[name] = local_session[name]
            changed = True
    if not changed:
        return remote_data
    return _dump_json(remote_session)


def strip_web_token(data: bytes) -> bytes:
    """web_token 是每台机器自己的本地访问口令，同步过去只会互相踢掉，直接不带。"""
    config = _load_json_object(data)
    if config is None or not config.get("web_token"):
        return data
    config["web_token"] = None
    return _dump_json(config)


def _load_json_object(data: bytes) -> dict | None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _dump_json(value: dict) -> bytes:
    # 和 SessionStore.save 一样缩进 1 格，文件看起来还是原来那个样子
    return json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")


def _human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"
