# 测试

分两套，职责不同：

| 目录 | 跑法 | 连不连网 | 什么时候跑 |
|---|---|---|---|
| `tests/mock/` | `make test` | 不连网，模型用 `ScriptedProvider` | 每次改代码、加功能都跑；**新功能必须补** |
| `tests/live/` | `make test-live` | 连真实免费 API | 改到模型接入、工具、主循环、CLI 流程时跑 |
| `tests/vendor_pocketflow/` | `make test` | 不连网 | 上游 PocketFlow 自带的测试，跟着 mock 一起跑 |

两套都用标准库 `unittest`（异步用 `IsolatedAsyncioTestCase`），没有 pytest 依赖。

## mock：快、离线、必须补

```bash
make test
make test PYTHON=~/env/s11/bin/python
```

- 不连网络、不读本机 `~/.pocketagent` / `~/.claude`：用 `tests/helpers.py` 里的
  `TemporaryDirectories` 和 `build_harness()`，每个测试一套临时工作区和 home
- 模型按剧本返回：`ScriptedProvider`（按顺序吐 `LLMResponse`），需要卡时序时用 `RoutingProvider`
- web 测试用 aiohttp 测试客户端走真实 HTTP / WebSocket；TUI 用 Textual 的 `run_test()`；
  没装可选依赖时自动跳过；沙箱测试只在有 `sandbox-exec` 的 macOS 上跑

**加新功能 / 新工具时**：在 `tests/mock/` 里加对应 `test_<功能>.py`，至少覆盖正常路径和一条出错路径。
新工具还要在 `default_tool_registry()` 注册后检查它的参数 schema、审批类型和结果落盘。
提交前跑一遍 `make test`。

## live：真实免费 API，默认跳过

```bash
make test-live          # = POCKETAGENT_LIVE=1 python -m unittest discover -s tests/live -t .
```

没有 `POCKETAGENT_LIVE=1` 时整组跳过，所以误跑全量测试也不会联网花钱。
`make test-live` 找模型的顺序：

1. `POCKETAGENT_LIVE_MODEL` + `POCKETAGENT_LIVE_BASE_URL`（CI 用环境变量注入）
2. `POCKETAGENT_LIVE_MODEL`（按名字去 `~/.pocketagent/config.json` 里找）
3. `POCKETAGENT_LIVE_PROFILE`（按 id / 名称 / 模型名指定配置里的模型）
4. 配置里第一个免费模型（模型名带 `:free`，或名称里写「免费」）

只自动挑免费模型，想用付费模型必须显式指定。没有可用模型时，模型相关用例跳过，
但网页 / 搜图这些不需要模型的用例照常跑。

覆盖的免费接口：

- 模型：OpenAI 兼容接口的 `:free` 模型 —— 流式文本、工具调用、usage
- agent 主循环：读文件、写文件、bash、`web_fetch`，顺带检查会话 / trace / 用量落盘
- 网页：`web_fetch` 读 example.com、必应搜索、必应搜图、下载图片
- 生图：智谱 `cogview-3-flash`（需要配置里有智谱密钥，没有就跳过）
- CLI：真的起 `python -m pocketagent` 子进程跑一句话，检查退出码和输出

写 live 用例的原则：**只校验流程，不校验模型的自由发挥**。比如断言「`read_file` 被调用了、
工具结果进了下一轮上下文、最终回答里有文件里的编号」，不要断言模型一定用某种语气、
一定并行发几个工具调用——免费模型的行为会变，那种断言只会带来假失败。

环境变量一览：

| 变量 | 用途 |
|---|---|
| `POCKETAGENT_LIVE` | 设成 `1` 才跑 live 套件（`make test-live` 会设） |
| `POCKETAGENT_LIVE_MODEL` / `_BASE_URL` / `_API_KEY` / `_PROTOCOL` | 直接指定模型接口，优先于配置文件 |
| `POCKETAGENT_LIVE_PROFILE` | 用配置文件里的某个模型（可指定付费模型） |
| `POCKETAGENT_LIVE_IMAGE_API_KEY` / `_IMAGE_BASE_URL` / `_IMAGE_MODEL` | 覆盖免费生图配置 |

## 目录

```
tests/
├── helpers.py               共享的临时目录、事件记录、假 agent 搭建
├── mock/                    快速离线用例（默认 make test）
├── live/                    真实免费 API 联调（make test-live）
└── vendor_pocketflow/       上游 PocketFlow 测试，别改
```
