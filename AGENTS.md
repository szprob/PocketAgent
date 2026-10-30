# 给在这个仓库里干活的 AI 编程助手

- `pocketflow/` 是原样复制的上游代码，不要改。要扩展它的行为就在 `pocketagent/` 里继承，见 `pocketflow/VENDORED.md`。
- 代码要人类可读：拆小函数、变量用全名、少用一行式推导 / lambda 分派 / 位置元组，多个返回值用 dataclass。
- 注释和文档用中文，标识符用英文；注释写「为什么」，不复述代码。
- 测试分两套，都用标准库 unittest（异步用 `IsolatedAsyncioTestCase`）：
  - `tests/mock/`：快速、不联网，模型用 `ScriptedProvider`。**新功能 / 新工具必须补这里的用例**，
    正常路径和一条出错路径都要覆盖；改完跑 `make test`（本机：`make test PYTHON=~/env/s11/bin/python`）。
  - `tests/live/`：调真实免费 API 校验流程，默认跳过，`make test-live` 才跑。只在不联网就验证不了的地方加，
    断言流程（工具被调用、结果进了下一轮）而不是模型的具体措辞。
- 新增工具放 `pocketagent/tools/`，在 `default_tool_registry()` 里注册，并补测试。
- 代码按职责分子包（docs/architecture.md「分层」一节）：主循环 `core/`、权限沙箱检查点 `safety/`、技能记忆 `knowledge/`、
  提问附件命令 `interaction/`、用量余额 `account/`、所有前端共用的后端 `server/`（接口按资源放 `server/routes/`）。
  不要再往 `pocketagent/` 顶层加模块；前端只走 `server/__init__.py` 里写的 REST + WebSocket 协议。
- 会话、trace 等运行产物写到 `~/.pocketagent/`，不要写进仓库目录。
