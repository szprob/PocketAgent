"""web 前端：无构建步骤的静态页面（static/），由 pocketagent/server 一起服务。"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
