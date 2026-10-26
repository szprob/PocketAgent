"""给界面用的文件浏览：工作区文件树、文件预览、选工作区时浏览本机目录。

只读。文件树和预览只允许在已登记的工作区里；选目录的浏览只列目录、不列文件。
"""

import mimetypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pocketagent.config import ConfigError
from pocketagent.tools.files import SKIPPED_DIRECTORY_NAMES

PREVIEW_LIMIT_BYTES = 400_000
LIST_LIMIT = 1000


@dataclass
class FileEntry:
    name: str
    # 相对工作区的路径（文件树）或绝对路径（选目录）
    path: str
    is_dir: bool
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_in_workspace(workspaces: list[str], workspace: str, relative_path: str) -> tuple[Path, Path]:
    """返回 (工作区根, 目标绝对路径)；工作区没登记、路径跑出工作区都报错。"""
    root = Path(workspace).expanduser().resolve()
    if str(root) not in workspaces:
        raise ConfigError(f"不是已添加的工作区：{workspace}")
    target = (root / relative_path).resolve()
    if not _is_within(target, root):
        raise ConfigError(f"路径不在工作区里：{relative_path}")
    return root, target


def _sorted_children(directory: Path) -> list[Path]:
    """目录在前、文件在后，各自按名字排；跳过 .git、node_modules 这类。"""
    children = [child for child in directory.iterdir() if child.name not in SKIPPED_DIRECTORY_NAMES]
    return sorted(children, key=lambda child: (not child.is_dir(), child.name.lower()))


def list_workspace_dir(workspaces: list[str], workspace: str, relative_path: str = "") -> list[dict[str, Any]]:
    root, directory = resolve_in_workspace(workspaces, workspace, relative_path)
    if not directory.is_dir():
        raise ConfigError(f"不是目录：{relative_path}")
    entries = []
    for child in _sorted_children(directory)[:LIST_LIMIT]:
        size = None if child.is_dir() else child.stat().st_size
        entries.append(FileEntry(child.name, str(child.relative_to(root)), child.is_dir(), size).to_dict())
    return entries


def read_workspace_file(workspaces: list[str], workspace: str, relative_path: str) -> dict[str, Any]:
    """文本文件给内容（太大只给开头）；图片告诉前端用原始地址显示；其他只给类型和大小。"""
    root, target = resolve_in_workspace(workspaces, workspace, relative_path)
    if not target.is_file():
        raise ConfigError(f"不是文件：{relative_path}")
    size = target.stat().st_size
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    result: dict[str, Any] = {"path": relative_path, "absolute_path": str(target), "size": size, "mime": mime}
    if mime.startswith("image/"):
        result["kind"] = "image"
        return result
    with target.open("rb") as file:
        data = file.read(PREVIEW_LIMIT_BYTES)
    try:
        result["content"] = data.decode("utf-8")
    except UnicodeDecodeError:
        result["kind"] = "binary"
        return result
    result["kind"] = "text"
    result["truncated"] = size > PREVIEW_LIMIT_BYTES
    return result


def workspace_file_path(workspaces: list[str], workspace: str, relative_path: str) -> Path:
    _, target = resolve_in_workspace(workspaces, workspace, relative_path)
    if not target.is_file():
        raise ConfigError(f"不是文件：{relative_path}")
    return target


def browse_directories(raw_path: str | None) -> dict[str, Any]:
    """选工作区用：列出某个目录下的子目录。默认从家目录开始。"""
    directory = Path(raw_path).expanduser() if raw_path else Path.home()
    directory = directory.resolve()
    if not directory.is_dir():
        raise ConfigError(f"目录不存在：{raw_path}")
    children = []
    try:
        candidates = sorted(directory.iterdir(), key=lambda child: child.name.lower())
    except PermissionError:
        raise ConfigError(f"没有权限读这个目录：{directory}")
    for child in candidates:
        if child.is_dir() and not child.name.startswith(".") and child.name not in SKIPPED_DIRECTORY_NAMES:
            children.append(FileEntry(child.name, str(child), True).to_dict())
    parent = str(directory.parent) if directory.parent != directory else None
    return {"path": str(directory), "parent": parent, "home": str(Path.home()), "directories": children[:LIST_LIMIT]}
