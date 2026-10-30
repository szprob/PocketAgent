"""命令行端到端联调：真的起 `python -m pocketagent` 子进程，用免费模型跑一句话。

用来确认配置加载、模型选择、流式输出、退出码整条链路是通的。用 `make test-live` 跑。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.live.support import (
    LIVE_MODEL,
    REPOSITORY_ROOT,
    live_model_profile_dict,
    skip_without_model,
)

CLI_TIMEOUT_SECONDS = 180


@skip_without_model
class LiveCommandLineTest(unittest.TestCase):
    def test_single_prompt_run(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        home = root / "home"
        workspace = root / "workspace"
        home.mkdir()
        workspace.mkdir()
        (home / "config.json").write_text(
            json.dumps(
                {
                    "models": [live_model_profile_dict(LIVE_MODEL)],
                    "default_model": "live",
                    "sandbox_enabled": False,
                    "skills_enabled": False,
                    "memory_enabled": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("POCKETAGENT_")
        }
        environment["POCKETAGENT_HOME"] = str(home)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pocketagent",
                "-C",
                str(workspace),
                "-m",
                "live",
                "-p",
                "不要调用工具，只回复：CLI-OK",
                "--mode",
                "full_auto",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )

        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("CLI-OK", result.stdout)
