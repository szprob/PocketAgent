"""记录一个会话改过哪些文件：右侧栏的「变更」列表、diff 和撤销都靠它。

只跟踪 edit_file / write_file：它们写文件前先通知这里，第一次碰到某个文件时把原始内容备份一份，
之后随时拿备份和磁盘上的现状比。bash 命令改的文件跟踪不到。
备份放在 ~/.pocketagent/checkpoints/<会话 id>/，不进工作目录。
"""

import difflib
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class FileChange:
    path: str
    display_path: str
    # added 新建 / modified 修改 / deleted 删除
    status: str
    additions: int = 0
    deletions: int = 0
    binary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode_text(data: bytes | None) -> str | None:
    """不是 UTF-8 文本（二进制文件）就返回 None。"""
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _diff_lines(old_text: str, new_text: str, display_path: str) -> list[str]:
    return list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
            lineterm="",
        )
    )


def _count_changed_lines(diff_lines: list[str]) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


class ChangeTracker:
    def __init__(self, directory: Path, workspace: Path) -> None:
        self.directory = directory
        self.workspace = workspace.resolve()
        self.index_path = directory / "index.json"
        # 绝对路径 → 原始内容的备份文件名；None 表示这个文件原来不存在（是新建的）
        self._originals: dict[str, str | None] = self._load_index()

    def _load_index(self) -> dict[str, str | None]:
        if not self.index_path.is_file():
            return {}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(self._originals, ensure_ascii=False, indent=1), encoding="utf-8")

    def display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def remember_original(self, path: Path) -> None:
        """写文件前调用。同一个文件只备份第一次的内容。"""
        key = str(path.resolve())
        if key in self._originals:
            return
        if path.is_file():
            backup_name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / backup_name).write_bytes(path.read_bytes())
            self._originals[key] = backup_name
        else:
            self._originals[key] = None
        self._save_index()

    def _original_bytes(self, key: str) -> bytes | None:
        backup_name = self._originals.get(key)
        if backup_name is None:
            return None
        return (self.directory / backup_name).read_bytes()

    @staticmethod
    def _current_bytes(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    def _describe(self, key: str) -> FileChange | None:
        """没有实际变化（改了又改回去）就返回 None。"""
        path = Path(key)
        original = self._original_bytes(key)
        current = self._current_bytes(path)
        if original == current:
            return None
        if original is None:
            status = "added"
        elif current is None:
            status = "deleted"
        else:
            status = "modified"
        display_path = self.display_path(path)
        old_text = _decode_text(original)
        new_text = _decode_text(current)
        if old_text is None or new_text is None:
            return FileChange(path=key, display_path=display_path, status=status, binary=True)
        additions, deletions = _count_changed_lines(_diff_lines(old_text, new_text, display_path))
        return FileChange(
            path=key, display_path=display_path, status=status, additions=additions, deletions=deletions
        )

    def list_changes(self) -> list[FileChange]:
        changes = []
        for key in self._originals:
            change = self._describe(key)
            if change is not None:
                changes.append(change)
        return changes

    def diff(self, path_text: str) -> str:
        key = str(Path(path_text).resolve())
        if key not in self._originals:
            raise KeyError(path_text)
        path = Path(key)
        old_text = _decode_text(self._original_bytes(key))
        new_text = _decode_text(self._current_bytes(path))
        if old_text is None or new_text is None:
            return "（二进制文件，不显示 diff）"
        return "\n".join(_diff_lines(old_text, new_text, self.display_path(path))) or "（没有变化）"

    def revert(self, path_text: str) -> None:
        """把文件恢复成第一次改之前的样子；原来不存在的就删掉。"""
        key = str(Path(path_text).resolve())
        if key not in self._originals:
            raise KeyError(path_text)
        path = Path(key)
        original = self._original_bytes(key)
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
