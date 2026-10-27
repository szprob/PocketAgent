"""临时任务的工作目录。

新任务可以不指定工作区：这时给它建一个空目录，任务在里面随便跑，不碰任何项目。
目录放在 ~/.pocketagent/scratch/<时间>-<随机>，和会话、附件、检查点放在一起。

但 ~/.pocketagent 整个是密钥目录（config.json 里有 API Key），沙箱默认连读都不让读，
所以要把临时任务的工作区单独放开，见 sandbox.SandboxPolicy.writable_inside_secret。

临时任务的产物就是临时的：删任务不会删这些文件，整个 scratch 目录可以随时手动清掉。
"""

import uuid
from datetime import datetime
from pathlib import Path

SCRATCH_DIRECTORY_NAME = "scratch"


def scratch_root(home_dir: Path) -> Path:
    return home_dir / SCRATCH_DIRECTORY_NAME


def create_scratch_workspace(home_dir: Path) -> Path:
    """给一个临时任务建目录。名字和会话 id 一样是「时间-随机」，一眼能看出先后。"""
    root = scratch_root(home_dir)
    root.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    directory = root / name
    directory.mkdir()
    return directory


def ensure_scratch_workspace(workspace: Path) -> Path:
    """重开旧任务时目录可能已经被清掉了（用户删了 scratch），重新建一个空的。

    不建的话工具在里面读写全是 ENOENT，用户看到的是莫名其妙的一串报错。
    """
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace
