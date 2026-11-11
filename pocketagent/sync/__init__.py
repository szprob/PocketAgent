"""云同步：把会话和项目记忆在几台机器之间对齐。

设计取舍见 docs/sync.md：默认只传「换台机器还想看到」的小数据，检查点 / trace / 工具输出 /
附件留在本机；存储后端可换（坚果云 WebDAV、iCloud 这类同步目录），不绑定某一家云。
"""

from pocketagent.sync.backend import LocalDirectoryBackend, SyncBackend, SyncError, WebDavBackend
from pocketagent.sync.config import BackendSettings, SyncConfig
from pocketagent.sync.engine import SyncEngine, SyncReport

__all__ = [
    "BackendSettings",
    "LocalDirectoryBackend",
    "SyncBackend",
    "SyncConfig",
    "SyncEngine",
    "SyncError",
    "SyncReport",
    "WebDavBackend",
]
