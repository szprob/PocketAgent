"""项目记忆：跨会话保留的笔记，一个工作区一份，放在 ~/.pocketagent/projects/<工作区路径>/memory/，不进仓库。

格式和 Claude Code 的 auto memory 一样（想和别的工具共用，把这个目录软链到同一个地方就行）：

    memory/
      MEMORY.md              索引，一行一条：- [标题](文件名.md) — 一句话
      user-prefers-uv.md     一条记忆一个文件，开头 --- 里写 name / description / type

每次开任务把 MEMORY.md 放进系统提示，具体某条用到再 read_file。模型用 write_file / edit_file 自己记，
写这个目录不用确认（只读模式除外），读也不用。
"""

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketagent.frontmatter import split_frontmatter

INDEX_FILE_NAME = "MEMORY.md"
MEMORY_TYPES = ["user", "feedback", "project", "reference"]
# 系统提示里 MEMORY.md 最多放这么多行、这么多字，超出的提示模型自己去读
INDEX_LINE_LIMIT = 200
INDEX_CHAR_LIMIT = 25_000
# 和 Claude Code 一样：字母数字以外的字符都换成短横线
_UNSAFE_PATH_CHARACTERS = re.compile(r"[^A-Za-z0-9]")


@dataclass
class MemoryFile:
    file: str
    name: str
    description: str
    type: str
    size: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_memory_dir(home_dir: Path, workspace: Path) -> Path:
    """和 Claude Code 一样用工作区路径当目录名：/Users/me/foo → -Users-me-foo。"""
    slug = _UNSAFE_PATH_CHARACTERS.sub("-", str(workspace.resolve()))
    return home_dir / "projects" / slug / "memory"


def read_index(memory_dir: Path) -> str:
    try:
        return (memory_dir / INDEX_FILE_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def memory_prompt_section(memory_dir: Path) -> str:
    index = read_index(memory_dir).strip()
    lines = index.splitlines()
    clipped = len(lines) > INDEX_LINE_LIMIT or len(index) > INDEX_CHAR_LIMIT
    shown = "\n".join(lines[:INDEX_LINE_LIMIT])[:INDEX_CHAR_LIMIT]
    if clipped:
        shown += f"\n（MEMORY.md 太长，后面的没放进来，需要时用 read_file 读 {memory_dir / INDEX_FILE_NAME}）"
    return f"""项目记忆：这个工作区有一个跨会话保留的记忆目录 {memory_dir}（不在工作目录里，读写都不用确认）。
- 一条记忆一个 .md 文件，开头写 ---、name（短横线连起来的英文短名）、description（一句话，以后靠它判断用不用得上）、type（{" / ".join(MEMORY_TYPES)}）、---，下面写正文
- 写完在 MEMORY.md 里加一行：- [标题](文件名.md) — 一句话。MEMORY.md 只放这种索引行，不放正文
- 值得记：用户是谁、偏好什么（user）；用户纠正或认可过的做法，带上原因（feedback）；代码和 git 记录里看不出来的进展、约束、决定，相对日期换成具体日期（project）；外部资源在哪（reference）
- 不要记：代码结构、git 历史里有的东西、只和这次对话有关的细节
- 写之前先看有没有已有的记忆可以更新，别重复；发现记错了就改掉或删掉
- 用户让你记住什么就记；觉得以后的任务会用得上也可以主动记，记了就在回复里说一声
- 用到某条记忆时，先确认它说的文件、函数还在，记忆反映的是写下那时的情况

MEMORY.md 现在的内容：
{shown or "（还是空的）"}"""


def list_memories(memory_dir: Path) -> list[MemoryFile]:
    if not memory_dir.is_dir():
        return []
    memories = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == INDEX_FILE_NAME:
            continue
        metadata, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        stat = path.stat()
        memories.append(
            MemoryFile(
                file=path.name,
                name=metadata.get("name") or path.stem,
                description=metadata.get("description", ""),
                type=metadata.get("type", ""),
                size=stat.st_size,
                updated_at=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            )
        )
    return memories


def memory_file_path(memory_dir: Path, file_name: str) -> Path:
    """只认目录里直接的 .md 文件，防止 ../ 跑出去。"""
    if not re.fullmatch(r"[^/\\]+\.md", file_name) or file_name.startswith("."):
        raise ValueError(f"不是记忆文件：{file_name}")
    return memory_dir / file_name


def delete_memory(memory_dir: Path, file_name: str) -> None:
    """删掉一条记忆，顺带去掉 MEMORY.md 里指向它的索引行。"""
    path = memory_file_path(memory_dir, file_name)
    path.unlink(missing_ok=True)
    if file_name == INDEX_FILE_NAME:
        return
    index_path = memory_dir / INDEX_FILE_NAME
    index = read_index(memory_dir)
    if not index:
        return
    kept = [line for line in index.splitlines() if f"]({file_name})" not in line]
    index_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
