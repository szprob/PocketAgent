"""同步主流程：比对本地、远端和上次同步的底稿，决定上传 / 下载 / 合并 / 删除。

判定只用三样东西：本地内容哈希、远端 ETag、上次同步记下的底稿哈希。
某一边和底稿一样，就说明这一边没动过，可以直接照另一边改；两边都动过才进合并。
没有底稿（第一次同步、或者换了设备）时按「两边都是新改动」处理，宁可在 conflicts/ 里多留一份。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pocketagent.sync.backend import LocalDirectoryBackend, RemoteEntry, SyncBackend, SyncError, WebDavBackend
from pocketagent.sync.config import SYNC_DIR_NAME, BackendSettings, SyncConfig, sync_state_dir
from pocketagent.sync.manifest import (
    ManifestEntry,
    collect_entries,
    content_hash,
    path_included,
    prepare_upload,
    restore_machine_fields,
)
from pocketagent.sync.merge import merge_by_kind

UPLOAD = "upload"
DOWNLOAD = "download"
MERGE = "merge"
DELETE_LOCAL = "delete-local"
DELETE_REMOTE = "delete-remote"
CONFLICT = "conflict"
BLOCKED = "blocked"
SKIP = "skip"

# 远端一次少了太多以前同步过的文件时先停下：多半是地址指错、账号换了或者目录被清空，
# 这时候按「远端删了」去删本地，损失不可逆。
REMOTE_MISSING_ABORT_RATIO = 0.5
REMOTE_MISSING_ABORT_MIN_FILES = 2


@dataclass
class FileState:
    base_hash: str
    remote_etag: str
    synced_at: str


@dataclass
class SyncState:
    files: dict[str, FileState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 状态文件坏了就当第一次同步：不会丢数据，只是多几次合并
            return cls()
        files: dict[str, FileState] = {}
        for relative_path, item in (data.get("files") or {}).items():
            if not isinstance(item, dict):
                continue
            files[relative_path] = FileState(
                base_hash=str(item.get("base_hash") or ""),
                remote_etag=str(item.get("remote_etag") or ""),
                synced_at=str(item.get("synced_at") or ""),
            )
        return cls(files=files)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "files": {
                relative_path: {
                    "base_hash": item.base_hash,
                    "remote_etag": item.remote_etag,
                    "synced_at": item.synced_at,
                }
                for relative_path, item in sorted(self.files.items())
            },
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)


class BaseStore:
    """上次同步时的本地内容，用来判断「哪一边没动」以及给文本合并当底稿。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, relative_path: str) -> Path:
        import hashlib

        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:32]
        return self.directory / digest

    def read(self, relative_path: str) -> bytes | None:
        path = self._path(relative_path)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def write(self, relative_path: str, data: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self._path(relative_path).with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(self._path(relative_path))

    def forget(self, relative_path: str) -> None:
        self._path(relative_path).unlink(missing_ok=True)


@dataclass
class SyncAction:
    kind: str
    path: str
    note: str = ""


@dataclass
class SyncReport:
    actions: list[SyncAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    local_files: int = 0
    remote_files: int = 0
    dry_run: bool = False

    def count(self, kind: str) -> int:
        return sum(1 for action in self.actions if action.kind == kind)

    def changed(self) -> bool:
        return any(action.kind not in (SKIP,) for action in self.actions)

    def lines(self) -> list[str]:
        lines = [
            f"本地 {self.local_files} 个文件，远端 {self.remote_files} 个文件",
            f"上传 {self.count(UPLOAD)} / 下载 {self.count(DOWNLOAD)} / 合并 {self.count(MERGE)}"
            f" / 删除本地 {self.count(DELETE_LOCAL)} / 删除远端 {self.count(DELETE_REMOTE)}",
        ]
        if self.count(CONFLICT):
            lines.append(f"冲突 {self.count(CONFLICT)}：远端版本另存在 {SYNC_DIR_NAME}/conflicts/")
        if self.count(BLOCKED):
            lines.append(f"跳过 {self.count(BLOCKED)}：只做了单向同步，另一边有改动")
        for action in self.actions:
            if action.kind == SKIP:
                continue
            label = {
                UPLOAD: "↑",
                DOWNLOAD: "↓",
                MERGE: "⇄",
                DELETE_LOCAL: "-本地",
                DELETE_REMOTE: "-远端",
                CONFLICT: "冲突",
                BLOCKED: "跳过",
            }.get(action.kind, action.kind)
            suffix = f"  {action.note}" if action.note else ""
            lines.append(f"  {label} {action.path}{suffix}")
        for item in self.skipped:
            lines.append(f"  ! 没同步 {item}")
        for error in self.errors:
            lines.append(f"  错误：{error}")
        return lines


class SyncEngine:
    def __init__(self, home_dir: Path, config: SyncConfig, backend: SyncBackend | None = None) -> None:
        self.home_dir = Path(home_dir)
        self.config = config
        self.backend = backend or create_backend(config.backend)
        state_dir = sync_state_dir(self.home_dir)
        self.state_path = state_dir / "state.json"
        self.base_store = BaseStore(state_dir / "base")
        self.conflict_dir = state_dir / "conflicts"

    def run(self, direction: str = "both", dry_run: bool = False) -> SyncReport:
        entries, skipped = collect_entries(self.home_dir, self.config.includes, self.config.max_file_bytes)
        local_entries = {entry.relative_path: entry for entry in entries}
        report = SyncReport(skipped=skipped, local_files=len(entries), dry_run=dry_run)
        state = SyncState.load(self.state_path)

        try:
            remote_entries = self._remote_files(report)
            self._guard_against_mass_deletion(remote_entries, state)
            managed = {
                path for path in set(local_entries) | set(state.files) if path_included(path, self.config.includes)
            }
            for path in remote_entries:
                if path_included(path, self.config.includes):
                    managed.add(path)
            for relative_path in sorted(managed):
                try:
                    self._sync_one(
                        relative_path,
                        local_entries.get(relative_path),
                        remote_entries.get(relative_path),
                        state,
                        report,
                        direction,
                        dry_run,
                    )
                except SyncError as error:
                    # 单个文件失败不拖累别的文件；状态里不会记它，下次还会重试
                    report.errors.append(f"{relative_path}：{error}")
        except SyncError as error:
            report.errors.append(str(error))
        finally:
            if not dry_run:
                state.save(self.state_path)
                self._write_device_record(report)
        return report

    def status(self) -> SyncReport:
        return self.run(direction="both", dry_run=True)

    def _remote_files(self, report: SyncReport) -> dict[str, RemoteEntry]:
        prefix = self.config.remote_prefix.rstrip("/") + "/"
        remote_entries: dict[str, RemoteEntry] = {}
        for entry in self.backend.list_files(self.config.remote_prefix):
            if not entry.path.startswith(prefix):
                continue
            remote_entries[entry.path[len(prefix) :]] = entry
        report.remote_files = len(remote_entries)
        return remote_entries

    def _guard_against_mass_deletion(self, remote_entries: dict, state: SyncState) -> None:
        known = [path for path in state.files if path_included(path, self.config.includes)]
        if len(known) < REMOTE_MISSING_ABORT_MIN_FILES:
            return
        missing = [path for path in known if path not in remote_entries]
        if len(missing) / len(known) <= REMOTE_MISSING_ABORT_RATIO:
            return
        raise SyncError(
            f"远端少了 {len(missing)}/{len(known)} 个以前同步过的文件，先确认地址和账号没指错；"
            "为了不误删本地，这次什么都没做"
        )

    def _sync_one(
        self,
        relative_path: str,
        entry: ManifestEntry | None,
        remote_entry: RemoteEntry | None,
        state: SyncState,
        report: SyncReport,
        direction: str,
        dry_run: bool,
    ) -> None:
        file_state = state.files.get(relative_path)
        if entry is None and remote_entry is None:
            # 两边都没了：清掉底稿就行，不用发请求
            if not dry_run:
                self.base_store.forget(relative_path)
                state.files.pop(relative_path, None)
            return
        local_data = entry.path.read_bytes() if entry is not None else None
        local_hash = content_hash(local_data) if local_data is not None else None
        local_changed = self._side_changed(local_hash, file_state.base_hash if file_state else None)
        remote_changed = self._side_changed(
            remote_entry.etag if remote_entry else None,
            file_state.remote_etag if file_state else None,
        )

        if entry is not None and remote_entry is not None:
            if not local_changed and not remote_changed:
                return
            if local_changed and not remote_changed:
                self._upload(relative_path, entry, local_data, local_hash, state, report, direction, dry_run)
                return
            if remote_changed and not local_changed:
                self._download(relative_path, remote_entry, local_data, state, report, direction, dry_run)
                return
            self._merge(relative_path, entry, remote_entry, local_data, state, report, direction, dry_run)
            return

        if entry is not None:
            # 远端没有这个文件
            if file_state is None:
                self._upload(relative_path, entry, local_data, local_hash, state, report, direction, dry_run)
            elif local_changed:
                # 远端删了、本地又改过：按不丢数据，重新传上去
                self._upload(
                    relative_path,
                    entry,
                    local_data,
                    local_hash,
                    state,
                    report,
                    direction,
                    dry_run,
                    "远端删过，本地有改动，重新上传",
                )
            else:
                self._delete_local(relative_path, state, report, direction, dry_run)
            return

        # 本地没有这个文件
        if file_state is None:
            self._download(relative_path, remote_entry, None, state, report, direction, dry_run)
        elif remote_changed:
            self._download(relative_path, remote_entry, None, state, report, direction, dry_run, "本地删过，远端有改动，重新拉回")
        else:
            self._delete_remote(relative_path, remote_entry, state, report, direction, dry_run)

    def _side_changed(self, current: str | None, base: str | None) -> bool:
        if current is None and base is None:
            return False
        if base is None:
            return True
        return current != base

    def _upload(
        self,
        relative_path: str,
        entry: ManifestEntry,
        local_data: bytes,
        local_hash: str,
        state: SyncState,
        report: SyncReport,
        direction: str,
        dry_run: bool,
        note: str = "",
    ) -> None:
        if direction == "pull":
            report.actions.append(SyncAction(BLOCKED, relative_path, "只拉取，本地有改动"))
            return
        report.actions.append(SyncAction(UPLOAD, relative_path, note))
        if dry_run:
            return
        payload = prepare_upload(entry, local_data)
        etag = self.backend.write(self._remote_path(relative_path), payload)
        self.base_store.write(relative_path, local_data)
        state.files[relative_path] = FileState(local_hash, etag, _now_text())

    def _download(
        self,
        relative_path: str,
        remote_entry: RemoteEntry,
        local_data: bytes | None,
        state: SyncState,
        report: SyncReport,
        direction: str,
        dry_run: bool,
        note: str = "",
    ) -> None:
        if direction == "push":
            report.actions.append(SyncAction(BLOCKED, relative_path, "只推送，远端有改动"))
            return
        report.actions.append(SyncAction(DOWNLOAD, relative_path, note))
        if dry_run:
            return
        data = self.backend.read(self._remote_path(relative_path))
        if relative_path.startswith("sessions/"):
            data = restore_machine_fields(data, local_data)
        self._write_local(relative_path, data)
        self.base_store.write(relative_path, data)
        state.files[relative_path] = FileState(content_hash(data), remote_entry.etag, _now_text())

    def _merge(
        self,
        relative_path: str,
        entry: ManifestEntry,
        remote_entry: RemoteEntry,
        local_data: bytes,
        state: SyncState,
        report: SyncReport,
        direction: str,
        dry_run: bool,
    ) -> None:
        if direction != "both":
            report.actions.append(SyncAction(BLOCKED, relative_path, "两边都改了，需要双向同步"))
            return
        remote_data = self.backend.read(self._remote_path(relative_path))
        base = self.base_store.read(relative_path)
        result = merge_by_kind(entry.kind, local_data, remote_data, base)
        if result.conflict:
            conflict_path = self._save_conflict(relative_path, remote_data, dry_run)
            report.actions.append(SyncAction(CONFLICT, relative_path, f"远端版本在 {conflict_path}"))
        report.actions.append(SyncAction(MERGE, relative_path, result.note))
        if dry_run:
            return
        merged = result.data
        if relative_path.startswith("sessions/"):
            merged = restore_machine_fields(merged, local_data)
        self._write_local(relative_path, merged)
        payload = prepare_upload(entry, merged)
        etag = self.backend.write(self._remote_path(relative_path), payload)
        self.base_store.write(relative_path, merged)
        state.files[relative_path] = FileState(content_hash(merged), etag, _now_text())

    def _delete_local(
        self, relative_path: str, state: SyncState, report: SyncReport, direction: str, dry_run: bool
    ) -> None:
        if direction == "push":
            report.actions.append(SyncAction(BLOCKED, relative_path, "只推送，远端已删除"))
            return
        report.actions.append(SyncAction(DELETE_LOCAL, relative_path))
        if dry_run:
            return
        (self.home_dir / relative_path).unlink(missing_ok=True)
        self.base_store.forget(relative_path)
        state.files.pop(relative_path, None)

    def _delete_remote(
        self,
        relative_path: str,
        remote_entry: RemoteEntry,
        state: SyncState,
        report: SyncReport,
        direction: str,
        dry_run: bool,
    ) -> None:
        if direction == "pull":
            report.actions.append(SyncAction(BLOCKED, relative_path, "只拉取，本地已删除"))
            return
        report.actions.append(SyncAction(DELETE_REMOTE, relative_path))
        if dry_run:
            return
        self.backend.delete(self._remote_path(relative_path))
        self.base_store.forget(relative_path)
        state.files.pop(relative_path, None)

    def _write_local(self, relative_path: str, data: bytes) -> None:
        target = self.home_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".sync-tmp")
        temporary.write_bytes(data)
        if relative_path.startswith("hooks/"):
            # WebDAV 不保存可执行位，钩子脚本落下来要还能直接跑
            temporary.chmod(0o755)
        temporary.replace(target)

    def _save_conflict(self, relative_path: str, remote_data: bytes, dry_run: bool) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{relative_path.replace('/', '__')}.{self.config.device_id}.{stamp}"
        path = self.conflict_dir / name
        if not dry_run:
            self.conflict_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(remote_data)
        return str(path)

    def _remote_path(self, relative_path: str) -> str:
        return f"{self.config.remote_prefix.rstrip('/')}/{relative_path}"

    def _write_device_record(self, report: SyncReport) -> None:
        payload = {
            "device_id": self.config.device_id,
            "last_sync_at": _now_text(),
            "host_files": report.local_files,
            "remote_files": report.remote_files,
            "uploaded": report.count(UPLOAD),
            "downloaded": report.count(DOWNLOAD),
            "merged": report.count(MERGE),
        }
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.backend.write(f"devices/{self.config.device_id}.json", body)
        except SyncError:
            # 心跳写不进去不影响数据已经同步完这件事
            pass


def create_backend(settings: BackendSettings) -> SyncBackend:
    if settings.kind == "local":
        return LocalDirectoryBackend(Path(settings.url).expanduser())
    password = settings.resolved_password()
    if not password:
        raise SyncError(
            "WebDAV 密码是空的：把密码写进 sync.json（pocketagent sync init --password ...），"
            f"或者设环境变量 {settings.password_env}"
        )
    return WebDavBackend(
        url=settings.url,
        username=settings.username,
        password=password,
        verify_tls=settings.verify_tls,
        timeout_seconds=settings.timeout_seconds,
    )


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")
