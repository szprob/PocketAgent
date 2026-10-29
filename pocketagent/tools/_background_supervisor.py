"""独立监督一个后台命令；只依赖标准库，可直接作为脚本启动。"""

import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=1)
    temporary.replace(path)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _update_metadata(path: Path, **changes) -> None:
    metadata = _load_json(path)
    metadata.update(changes)
    _write_json(path, metadata)


def main(config_path: Path) -> int:
    config = _load_json(config_path)
    metadata_path = Path(config["metadata_path"])
    output_path = Path(config["output_path"])
    stop_request_path = Path(config["stop_request_path"])
    _update_metadata(metadata_path, supervisor_pid=os.getpid())
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        if config.get("sandbox_note"):
            output.write((config["sandbox_note"] + "\n").encode("utf-8"))
            output.flush()
        try:
            process = subprocess.Popen(
                config["command_line"],
                cwd=config["workspace"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as error:
            message = f"后台命令启动失败：{type(error).__name__}: {error}"
            output.write((message + "\n").encode("utf-8"))
            _update_metadata(
                metadata_path,
                status="error",
                error=message,
                finished_at=_now_text(),
            )
            return 1
        _update_metadata(
            metadata_path,
            status="running",
            process_pid=process.pid,
            started_at=_now_text(),
        )
        exit_code = process.wait()
        status = "stopped" if stop_request_path.exists() else "exited"
        _update_metadata(
            metadata_path,
            status=status,
            exit_code=exit_code,
            finished_at=_now_text(),
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
