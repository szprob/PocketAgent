"""工具输出落盘：超过 max_tool_output_chars 的完整输出写到 ~/.pocketagent/tool_outputs/<会话>/。

放在 PocketAgent 自己的目录里，不进工作区，所以影子 git、变更列表、撤销都看不到它。
这个目录会加进 ToolContext.extra_read_dirs：模型用 read_file / grep 读它不用审批，bash 沙箱也放行。
"""

import os
import re
import time
from pathlib import Path

MAX_FILE_BYTES = 20_000_000
KEEP_DAYS = 7
PRUNE_INTERVAL_SECONDS = 3600
_UNSAFE_FILENAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")
_last_prune_at = 0.0


def safe_name(value: str, fallback: str = "output") -> str:
    """会话 id / 工具调用 id 拿来当文件名：只留字母数字和 . _ -，防止跑出目录。"""
    name = _UNSAFE_FILENAME_CHARACTERS.sub("_", value).strip("._-")
    return name[:80] or fallback


def session_directory(root: Path, session_id: str) -> Path:
    return root / safe_name(session_id, "session")


def save_tool_output(directory: Path, call_id: str, text: str) -> Path | None:
    """把完整输出写进会话目录，返回路径；写不了（磁盘满、没权限）返回 None，不影响工具结果。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe_name(call_id, 'tool')}.log"
        payload = _capped_text(text)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(payload)
    except OSError:
        return None
    _prune_old_outputs(directory.parent)
    return path


def _capped_text(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_FILE_BYTES:
        return text
    head = encoded[:MAX_FILE_BYTES].decode("utf-8", "ignore")
    return f"{head}\n\n（完整输出超过 {MAX_FILE_BYTES // 1_000_000}MB，后面没有保存）"


def _prune_old_outputs(root: Path) -> None:
    """每小时最多扫一次，删掉 7 天前的日志；删任务时还会整个目录一起删。"""
    global _last_prune_at
    now = time.monotonic()
    if now - _last_prune_at < PRUNE_INTERVAL_SECONDS:
        return
    _last_prune_at = now
    if not root.is_dir():
        return
    cutoff = time.time() - KEEP_DAYS * 24 * 60 * 60
    for session_path in root.iterdir():
        if not session_path.is_dir():
            continue
        for path in session_path.glob("*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
