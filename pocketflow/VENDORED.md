# pocketflow（原样复制的上游代码）

- 来源：https://github.com/The-Pocket/PocketFlow
- 版本：main 分支 commit `f74d023f93607b8c3268133339a5e532a949898c`（2026-07-26）
- 复制的文件：`pocketflow/__init__.py`、`pocketflow/__init__.pyi`、`LICENSE`（MIT）
- 上游测试原样放在 `tests/vendor_pocketflow/`

约定：这个目录不改。要扩展 PocketFlow 的行为（trace、流式事件等），就在 `pocketagent/` 里继承它的类，
例如 `pocketagent/nodes.py` 里的 `AgentNode` 覆盖了 `_run_async` 来发节点开始/结束事件。
升级上游时把上面三个文件和测试一起换掉，再跑一遍 `make test`。
