"""同步配置：按机器分开存在 config.json 里，本机只留身份和密码。

config.json 会跟着云同步走，所以同步设置不能只存一份——两台机器各自改自己的，
后同步的会把前一台覆盖掉。这里按设备 id 分开存：

    {
      "sync": {
        "devices": {
          "CK-L220116-4a9f56": {
            "backend": {"kind": "webdav", "url": "...", "username": "..."},
            "includes": ["sessions", "projects", "config"]
          }
        }
      }
    }

密码不写进 config.json（那会被传到云端），只留在本机的 ~/.pocketagent/sync.json，
权限 0600。这个文件还存本机的 device_id——它就是上面那张表的键。
"""

import json
import os
import re
import socket
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketagent.config import config_path
from pocketagent.sync.backend import normalize_repo

SYNC_CONFIG_NAME = "sync.json"
SYNC_DIR_NAME = "sync"
# 后端类型：github = 私有仓库（默认）；webdav = 坚果云这类支持 WebDAV 的网盘；
# local = 已经在同步的目录（iCloud、OneDrive、NAS、自己的 git 仓库）
BACKEND_KINDS = ("webdav", "local", "github")
# 可以打开同步的数据集。默认只有会话和项目记忆，其余都带密钥、绝对路径或者会自己跑起来
INCLUDE_CHOICES = ("sessions", "projects", "config", "automations", "mcp", "hooks")
DEFAULT_INCLUDES = ("sessions", "projects")
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_REMOTE_PREFIX = "data"
# github 仓库是通用的：数据自动放进仓库下的 pocketagent/ 目录，不占仓库根
GITHUB_REMOTE_PREFIX = "pocketagent"
DEFAULT_PASSWORD_ENV = "POCKETAGENT_SYNC_PASSWORD"


def default_remote_prefix(kind: str) -> str:
    """数据在远端放在哪：repo 自动用 pocketagent/，直接给目录就用目录本身。"""
    if kind == "github":
        return GITHUB_REMOTE_PREFIX
    if kind == "local":
        return ""
    return DEFAULT_REMOTE_PREFIX


class SyncConfigError(Exception):
    """配置读不出来或者写得不对。"""


@dataclass
class BackendSettings:
    kind: str = "github"
    # webdav 时是仓库地址（坚果云 https://dav.jianguoyun.com/dav/<目录>）；local 时是目录
    url: str = ""
    username: str = ""
    # 密码只在身份文件里，不跟着 config.json 上云
    password: str = ""
    password_env: str = DEFAULT_PASSWORD_ENV
    verify_tls: bool = True
    timeout_seconds: float = 30.0
    # 只有 github 用：推到哪个分支
    branch: str = "main"

    def resolved_password(self) -> str:
        return os.environ.get(self.password_env, "") or self.password

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackendSettings":
        kind = str(data.get("kind") or "github").strip()
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
            password_env=str(data.get("password_env") or DEFAULT_PASSWORD_ENV),
            verify_tls=bool(data.get("verify_tls", True)),
            timeout_seconds=float(data.get("timeout_seconds") or 30.0),
            branch=str(data.get("branch") or "main"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        """写进 config.json 的部分：不含密码。"""
        return {
            "kind": self.kind,
            "url": self.url,
            "username": self.username,
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
            "branch": self.branch,
        }


@dataclass
class LocalIdentity:
    """本机身份：设备 id 和密码，只在 ~/.pocketagent/sync.json 里。"""

    device_id: str
    password: str = ""
    password_env: str = DEFAULT_PASSWORD_ENV
    # 旧版 sync.json 里存着完整配置，第一次读的时候当迁移来源
    legacy: dict[str, Any] | None = None


@dataclass
class SyncConfig:
    device_id: str
    backend: BackendSettings
    includes: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDES))
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    remote_prefix: str = ""

    def __post_init__(self) -> None:
        if self.backend.kind == "github":
            # 存成 owner/repo，别把整条 GitHub URL 写进配置
            self.backend.url = normalize_repo(self.backend.url)
        if not self.remote_prefix:
            # 没显式给就按后端类型定：repo 自动用 pocketagent/，local 直接用目录本身
            self.remote_prefix = default_remote_prefix(self.backend.kind)

    # ---- 读 ----

    @classmethod
    def load(cls, home_dir: Path) -> "SyncConfig":
        home_dir = Path(home_dir)
        identity = read_identity(home_dir)
        if identity is None:
            raise SyncConfigError(
                f"还没配置同步：先跑 pocketagent sync init（配置文件是 {sync_config_path(home_dir)}）"
            )
        entry = _read_device_entry(home_dir, identity.device_id)
        if entry is None and identity.legacy:
            # 旧版：设置全在 sync.json 里，这次读出来照常用，下次保存时自动搬进 config.json
            entry = identity.legacy
        if entry is None:
            raise SyncConfigError("这台机器还没有同步设置：在设置 → 云同步里重填一次")
        data = dict(entry)
        backend = dict(entry.get("backend") or {})
        backend["password"] = identity.password
        backend["password_env"] = identity.password_env
        data["backend"] = backend
        data["device_id"] = identity.device_id
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncConfig":
        backend_data = data.get("backend") or {}
        if not isinstance(backend_data, dict):
            raise SyncConfigError("backend 应该是一个对象")
        backend = BackendSettings.from_dict(backend_data)
        if backend.kind == "github":
            try:
                backend.url = normalize_repo(backend.url)
            except Exception as error:
                raise SyncConfigError(str(error))
        includes = [str(item) for item in data.get("includes") or DEFAULT_INCLUDES]
        unknown = [item for item in includes if item not in INCLUDE_CHOICES]
        if unknown:
            raise SyncConfigError(f"includes 里有不认识的项：{', '.join(unknown)}")
        if not includes:
            raise SyncConfigError("同步范围至少要选一项")
        max_file_bytes = int(data.get("max_file_bytes") or DEFAULT_MAX_FILE_BYTES)
        if max_file_bytes <= 0:
            raise SyncConfigError("max_file_bytes 要大于 0")
        device_id = str(data.get("device_id") or "").strip() or make_device_id()
        remote_prefix = str(data.get("remote_prefix") or "").strip().strip("/")
        return cls(
            device_id=device_id,
            backend=backend,
            includes=list(dict.fromkeys(includes)),
            max_file_bytes=max_file_bytes,
            remote_prefix=remote_prefix,
        )

    # ---- 写 ----

    def device_entry(self) -> dict[str, Any]:
        """写进 config.json 的那一份（没有密码）。"""
        return {
            "backend": self.backend.to_public_dict(),
            "includes": self.includes,
            "max_file_bytes": self.max_file_bytes,
            "remote_prefix": self.remote_prefix,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def save(self, home_dir: Path) -> None:
        """命令行用：把设置写进 config.json，再更新本机身份文件。"""
        home_dir = Path(home_dir)
        _write_device_entry(home_dir, self.device_id, self.device_entry())
        self.save_identity(home_dir)

    def save_identity(self, home_dir: Path) -> None:
        """只写本机身份和密码；web 端保存 config.json 那一半由 SessionManager 负责。"""
        path = sync_config_path(Path(home_dir))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "device_id": self.device_id,
            "password": self.backend.password,
            "password_env": self.backend.password_env,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)


def sync_config_path(home_dir: Path) -> Path:
    """本机身份文件（device_id + 密码），不是完整的同步设置。"""
    return Path(home_dir) / SYNC_CONFIG_NAME


def sync_state_dir(home_dir: Path) -> Path:
    return Path(home_dir) / SYNC_DIR_NAME


def read_identity(home_dir: Path) -> LocalIdentity | None:
    path = sync_config_path(home_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SyncConfigError(f"{path} 不是合法 JSON")
    if not isinstance(data, dict):
        raise SyncConfigError(f"{path} 应该是一个 JSON 对象")
    backend = data.get("backend")
    password = str(data.get("password") or "")
    password_env = str(data.get("password_env") or DEFAULT_PASSWORD_ENV)
    legacy = None
    if isinstance(backend, dict):
        # version 1：完整设置都在这里，密码在 backend 里
        password = password or str(backend.get("password") or "")
        password_env = str(backend.get("password_env") or password_env)
        if backend.get("url"):
            legacy = {
                "backend": {
                    "kind": backend.get("kind") or "webdav",
                    "url": backend.get("url"),
                    "username": backend.get("username") or "",
                    "verify_tls": backend.get("verify_tls", True),
                    "timeout_seconds": backend.get("timeout_seconds", 30.0),
                },
                "includes": data.get("includes") or list(DEFAULT_INCLUDES),
                "max_file_bytes": data.get("max_file_bytes") or DEFAULT_MAX_FILE_BYTES,
                "remote_prefix": data.get("remote_prefix") or DEFAULT_REMOTE_PREFIX,
            }
    return LocalIdentity(
        device_id=str(data.get("device_id") or "").strip() or make_device_id(),
        password=password,
        password_env=password_env,
        legacy=legacy,
    )


def default_local_directory(home: Path | None = None) -> Path:
    """本地同步目录的默认值：装了坚果云就用它的同步文件夹，其次 iCloud 云盘。"""
    base = Path(home).expanduser() if home is not None else Path.home()
    if sys.platform == "darwin":
        nutstore = base / "Nutstore Files"
        if nutstore.is_dir():
            return nutstore / "pocketagent"
        icloud = base / "Library/Mobile Documents/com~apple~CloudDocs"
        if icloud.is_dir():
            return icloud / "PocketAgent"
    return base / "PocketAgent"


def _read_device_entry(home_dir: Path, device_id: str) -> dict[str, Any] | None:
    path = config_path(home_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sync_section = data.get("sync")
    if not isinstance(sync_section, dict):
        return None
    devices = sync_section.get("devices")
    if not isinstance(devices, dict):
        return None
    entry = devices.get(device_id)
    return entry if isinstance(entry, dict) else None


def _write_device_entry(home_dir: Path, device_id: str, entry: dict[str, Any]) -> None:
    path = config_path(home_dir)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SyncConfigError(f"{path} 不是合法 JSON，先修好再保存同步设置")
        if isinstance(loaded, dict):
            data = loaded
    sync_section = data.get("sync")
    if not isinstance(sync_section, dict):
        sync_section = {}
    devices = sync_section.get("devices")
    if not isinstance(devices, dict):
        devices = {}
    devices[device_id] = entry
    sync_section["devices"] = devices
    data["sync"] = sync_section
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def make_device_id() -> str:
    """主机名 + 随机后缀：两台机器同名时也能区分开，名字只用来给人看。"""
    host = socket.gethostname().split(".")[0]
    host = re.sub(r"[^0-9A-Za-z_-]+", "-", host).strip("-")[:24] or "device"
    return f"{host}-{uuid.uuid4().hex[:6]}"
