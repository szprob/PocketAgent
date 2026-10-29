"""可持续后台命令：独立监督进程、磁盘日志，以及查询 / 等待 / 停止。"""

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
from datetime import datetime
from typing import Any

from pocketagent.safety.processes import process_exists, signal_process_tree

START_TIMEOUT_SECONDS = 10
DEFAULT_READ_LIMIT_BYTES = 16_384
MAX_READ_LIMIT_BYTES = 65_536
ACTIVE_STATUSES = {"starting", "running"}


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=1)
    temporary.replace(path)


class BackgroundTaskManager:
    """每条命令由独立 Python 监督进程等待，所以 PocketAgent 退出也不会带走命令。"""

    def __init__(self, root: Path, workspace: Path, session_id: str) -> None:
        self.root = root
        self.workspace = workspace.resolve()
        self.session_id = session_id
        self._reapers: set[asyncio.Task] = set()

    def _directory(self, task_id: str) -> Path:
        if not task_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in task_id):
            raise ValueError("后台任务 id 不合法")
        return self.root / task_id

    def _metadata_path(self, task_id: str) -> Path:
        return self._directory(task_id) / "meta.json"

    def _load(self, task_id: str) -> dict[str, Any]:
        path = self._metadata_path(task_id)
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"后台任务不存在或状态不可读：{task_id}") from error
        if Path(metadata["workspace"]).resolve() != self.workspace:
            raise ValueError("这个后台任务属于另一个工作区")
        return metadata

    def _public_record(self, metadata: dict[str, Any]) -> dict[str, Any]:
        task_id = metadata["id"]
        record = {
            key: metadata.get(key)
            for key in (
                "id",
                "session_id",
                "command",
                "status",
                "created_at",
                "started_at",
                "finished_at",
                "exit_code",
                "error",
            )
        }
        record["workspace"] = metadata["workspace"]
        record["stopping"] = (self._directory(task_id) / "stop.requested").exists()
        supervisor_pid = metadata.get("supervisor_pid")
        if record["status"] in ACTIVE_STATUSES and supervisor_pid and not process_exists(supervisor_pid):
            record["status"] = "unknown"
        try:
            record["output_bytes"] = (self._directory(task_id) / "output.log").stat().st_size
        except OSError:
            record["output_bytes"] = 0
        return record

    async def start(self, command: str, command_line: list[str], sandbox_note: str = "") -> dict[str, Any]:
        task_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        directory = self._directory(task_id)
        directory.mkdir(parents=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        metadata_path = self._metadata_path(task_id)
        output_path = directory / "output.log"
        stop_request_path = directory / "stop.requested"
        metadata = {
            "id": task_id,
            "workspace": str(self.workspace),
            "session_id": self.session_id,
            "command": command,
            "status": "starting",
            "created_at": _now_text(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "supervisor_pid": None,
            "process_pid": None,
            "error": None,
        }
        _write_json(metadata_path, metadata)
        output_path.touch(mode=0o600, exist_ok=True)
        config = {
            "workspace": str(self.workspace),
            "command_line": command_line,
            "metadata_path": str(metadata_path),
            "output_path": str(output_path),
            "stop_request_path": str(stop_request_path),
            "sandbox_note": sandbox_note,
        }
        config_path = directory / "config.json"
        _write_json(config_path, config)
        supervisor_script = Path(__file__).with_name("_background_supervisor.py")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(supervisor_script),
            str(config_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        reaper = asyncio.create_task(self._reap_supervisor(process))
        self._reapers.add(reaper)
        reaper.add_done_callback(self._reapers.discard)
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            current = self._load(task_id)
            if current.get("status") not in ACTIVE_STATUSES or current.get("process_pid"):
                return self._public_record(current)
            await asyncio.sleep(0.05)
        stop_request_path.touch(exist_ok=True)
        if process.returncode is None:
            process.kill()
        raise TimeoutError("后台命令启动状态迟迟未落盘；已停止监督进程，请检查后台任务目录")

    async def _reap_supervisor(self, process: asyncio.subprocess.Process) -> None:
        await process.wait()

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        records = []
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            try:
                metadata = self._load(directory.name)
            except (ValueError, KeyError, OSError):
                continue
            records.append(self._public_record(metadata))
        records.sort(key=lambda record: record.get("created_at") or "", reverse=True)
        return records

    def status(self, task_id: str) -> dict[str, Any]:
        return self._public_record(self._load(task_id))

    def read(self, task_id: str, offset: int = 0, limit_bytes: int = DEFAULT_READ_LIMIT_BYTES) -> dict[str, Any]:
        record = self.status(task_id)
        offset = max(0, int(offset))
        limit_bytes = min(max(1, int(limit_bytes)), MAX_READ_LIMIT_BYTES)
        output_path = self._directory(task_id) / "output.log"
        with output_path.open("rb") as file:
            file.seek(offset)
            data = file.read(limit_bytes)
        next_offset = offset + len(data)
        return {
            **record,
            "offset": offset,
            "next_offset": next_offset,
            "output": data.decode("utf-8", errors="replace"),
            "has_more": next_offset < record["output_bytes"],
        }

    async def wait(self, task_id: str, timeout_seconds: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(0, timeout_seconds)
        while True:
            record = self.status(task_id)
            if record["status"] not in ACTIVE_STATUSES:
                return record
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return record
            await asyncio.sleep(min(0.2, remaining))

    async def stop(self, task_id: str, grace_seconds: float = 10) -> dict[str, Any]:
        metadata = self._load(task_id)
        record = self._public_record(metadata)
        if record["status"] not in ACTIVE_STATUSES:
            return record
        stop_request_path = self._directory(task_id) / "stop.requested"
        stop_request_path.touch(exist_ok=True)
        process_pid = metadata.get("process_pid")
        if not process_pid:
            await asyncio.sleep(0.2)
            metadata = self._load(task_id)
            process_pid = metadata.get("process_pid")
        if process_pid:
            await signal_process_tree(process_pid, force=False)
        deadline = asyncio.get_running_loop().time() + max(0, grace_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if self.status(task_id)["status"] not in ACTIVE_STATUSES:
                return await self._wait_for_supervisor(task_id)
            await asyncio.sleep(0.1)
        if process_pid:
            await signal_process_tree(process_pid, force=True)
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if self.status(task_id)["status"] not in ACTIVE_STATUSES:
                return await self._wait_for_supervisor(task_id)
            await asyncio.sleep(0.1)
        return self.status(task_id)

    async def _wait_for_supervisor(self, task_id: str) -> dict[str, Any]:
        """最终状态先于监督进程退出落盘；等它收尾，避免调用方马上关事件循环时留下管道。"""
        supervisor_pid = self._load(task_id).get("supervisor_pid")
        for _ in range(50):
            if not supervisor_pid or not process_exists(supervisor_pid):
                break
            await asyncio.sleep(0.02)
        return self.status(task_id)
