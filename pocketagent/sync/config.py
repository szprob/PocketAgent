"""同步配置：~/.pocketagent/sync.json。

和设备绑定（device_id 各机器不同），所以单独一个文件，不塞进 config.json——那个文件会被
用户手动编辑、也会在设置页里整份保存，混进来容易被覆盖掉。文件里有云端凭据，落盘时 0600。
"""

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SYNC_CONFIG_NAME = "sync.json"
SYNC_DIR_NAME = "sync"
# 后端类型：webdav = 坚果云这类支持 WebDAV 的网盘；local = 已经在同步的目录（iCloud、OneDrive、NAS）
BACKEND_KINDS = ("webdav", "local")
# 可以打开同步的数据集。默认只有会话和项目记忆，其余都带密钥、绝对路径或者会自己跑起来
INCLUDE_CHOICES = ("sessions", "projects", "config", "automations", "mcp", "hooks")
DEFAULT_INCLUDES = ("sessions", "projects")
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_REMOTE_PREFIX = "data"


class SyncConfigError(Exception):
    """配置读不出来或者写得不对。"""


@dataclass
class BackendSettings:
    kind: str = "webdav"
    # webdav 时是仓库地址（坚果云 https://dav.jianguoyun.com/dav/<目录>）；local 时是目录
    url: str = ""
    username: str = ""
    # 明文密码只在文件权限保护下存着；更推荐用环境变量，见 resolved_password
    password: str = ""
    password_env: str = "POCKETAGENT_SYNC_PASSWORD"
    verify_tls: bool = True
    timeout_seconds: float = 30.0

    def resolved_password(self) -> str:
        return os.environ.get(self.password_env, "") or self.password

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackendSettings":
        kind = str(data.get("kind") or "webdav").strip()
        if kind not in BACKEND_KINDS:
            raise SyncConfigError(f"backend.kind 只能是 {' / '.join(BACKEND_KINDS)}，现在是 {kind!r}")
        url = str(data.get("url") or "").strip()
        if not url:
            raise SyncConfigError("backend.url 不能为空：webdav 写仓库地址，local 写目录")
        return cls(
            kind=kind,
            url=url,
            username=str(data.get("username") or ""),
            password=str(data.get("password") or ""),
            password_env=str(data.get("password_env") or "POCKETAGENT_SYNC_PASSWORD"),
            verify_tls=bool(data.get("verify_tls", True)),
            timeout_seconds=float(data.get("timeout_seconds") or 30.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "username": self.username,
            "password": self.password,
            "password_env": self.password_env,
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class SyncConfig:
    device_id: str
    backend: BackendSettings
    includes: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDES))
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    # 云端根目录下再套一层，避免用户把地址指到共享目录时和别的文件混在一起
    remote_prefix: str = DEFAULT_REMOTE_PREFIX
    # web / 桌面端在后台每隔多少分钟自动同步一次；0 = 只手动跑
    auto_interval_minutes: int = 10

    @classmethod
    def load(cls, path: Path) -> "SyncConfig":
        if not path.is_file():
            raise SyncConfigError(f"还没配置同步：先跑 pocketagent sync init（配置文件是 {path}）")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SyncConfigError(f"{path} 不是合法 JSON：{error}")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncConfig":
        backend_data = data.get("backend") or {}
        if not isinstance(backend_data, dict):
            raise SyncConfigError("backend 应该是一个对象")
        backend = BackendSettings.from_dict(backend_data)
        includes = [str(item) for item in data.get("includes") or DEFAULT_INCLUDES]
        unknown = [item for item in includes if item not in INCLUDE_CHOICES]
        if unknown:
            raise SyncConfigError(f"includes 里有不认识的项：{', '.join(unknown)}")
        max_file_bytes = int(data.get("max_file_bytes") or DEFAULT_MAX_FILE_BYTES)
        if max_file_bytes <= 0:
            raise SyncConfigError("max_file_bytes 要大于 0")
        device_id = str(data.get("device_id") or "").strip() or make_device_id()
        remote_prefix = str(data.get("remote_prefix") or DEFAULT_REMOTE_PREFIX).strip("/")
        if not remote_prefix:
            raise SyncConfigError("remote_prefix 不能为空")
        auto_interval = data.get("auto_interval_minutes", 10)
        try:
            auto_interval_minutes = int(auto_interval)
        except (TypeError, ValueError):
            raise SyncConfigError(f"auto_interval_minutes 要是一个整数：{auto_interval!r}")
        if auto_interval_minutes < 0:
            raise SyncConfigError("auto_interval_minutes 不能小于 0")
        return cls(
            device_id=device_id,
            backend=backend,
            includes=list(dict.fromkeys(includes)),
            max_file_bytes=max_file_bytes,
            remote_prefix=remote_prefix,
            auto_interval_minutes=auto_interval_minutes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "device_id": self.device_id,
            "backend": self.backend.to_dict(),
            "includes": self.includes,
            "max_file_bytes": self.max_file_bytes,
            "remote_prefix": self.remote_prefix,
            "auto_interval_minutes": self.auto_interval_minutes,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        # 里面有密码
        temporary.chmod(0o600)
        temporary.replace(path)


def sync_config_path(home_dir: Path) -> Path:
    return home_dir / SYNC_CONFIG_NAME


def sync_state_dir(home_dir: Path) -> Path:
    return home_dir / SYNC_DIR_NAME


def make_device_id() -> str:
    """主机名 + 随机后缀：两台机器同名时也能区分开，名字只用来给人看。"""
    host = socket.gethostname().split(".")[0]
    host = re.sub(r"[^0-9A-Za-z_-]+", "-", host).strip("-")[:24] or "device"
    return f"{host}-{uuid.uuid4().hex[:6]}"
