"""为单个任务创建独立 git worktree；不复制原工作区的未提交修改。"""

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess

from pocketagent.config import ConfigError


@dataclass
class WorktreeInfo:
    path: str
    root: str
    origin: str
    origin_repo_root: str
    branch: str
    base_commit: str
    git_dirs: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知 git 错误"
        raise ConfigError(f"创建 worktree 失败：{detail}")
    return result


def _git_text(repository: Path, *arguments: str) -> str:
    return _git(repository, *arguments).stdout.strip()


def _absolute_git_path(worktree_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = worktree_root / path
    return path.resolve()


def create_worktree(workspace: Path, home_dir: Path, session_id: str) -> WorktreeInfo:
    origin = workspace.expanduser().resolve()
    if not origin.is_dir():
        raise ConfigError(f"工作区不存在：{origin}")
    inside_repository = _git(origin, "rev-parse", "--is-inside-work-tree", check=False)
    if inside_repository.returncode != 0 or inside_repository.stdout.strip() != "true":
        raise ConfigError("独立 worktree 只支持普通 git 仓库（当前目录不是 git 工作区）")
    if _git_text(origin, "rev-parse", "--is-bare-repository") == "true":
        raise ConfigError("裸仓库不能创建任务 worktree")
    if _git_text(origin, "rev-parse", "--show-superproject-working-tree"):
        raise ConfigError("暂不支持直接为子模块创建任务 worktree；请选择外层仓库")
    try:
        base_commit = _git_text(origin, "rev-parse", "--verify", "HEAD")
    except ConfigError as error:
        raise ConfigError("仓库还没有提交，无法从 HEAD 创建 worktree") from error

    repository_root = Path(_git_text(origin, "rev-parse", "--show-toplevel")).resolve()
    prefix = _git_text(origin, "rev-parse", "--show-prefix")
    branch = f"pocketagent/{session_id}"
    if _git(repository_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise ConfigError(f"分支已经存在：{branch}")
    worktree_root = home_dir.expanduser().resolve() / "worktrees" / session_id
    if worktree_root.exists():
        raise ConfigError(f"任务 worktree 目录已经存在：{worktree_root}")
    worktree_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(repository_root, "worktree", "add", "-b", branch, str(worktree_root), base_commit)
        effective_workspace = (worktree_root / prefix).resolve()
        if not effective_workspace.is_dir():
            raise ConfigError(f"原工作区子目录在 worktree 中不存在：{prefix}")
        git_dir = _absolute_git_path(worktree_root, _git_text(worktree_root, "rev-parse", "--git-dir"))
        common_dir = _absolute_git_path(worktree_root, _git_text(worktree_root, "rev-parse", "--git-common-dir"))
        git_dirs = list(dict.fromkeys([str(git_dir), str(common_dir)]))
        return WorktreeInfo(
            path=str(effective_workspace),
            root=str(worktree_root),
            origin=str(origin),
            origin_repo_root=str(repository_root),
            branch=branch,
            base_commit=base_commit,
            git_dirs=git_dirs,
        )
    except Exception:
        if worktree_root.exists():
            _git(repository_root, "worktree", "remove", "--force", str(worktree_root), check=False)
        if _git(repository_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
            _git(repository_root, "branch", "-D", branch, check=False)
        raise
