# 配置

配置存在 `~/.pocketagent/config.json`（文件权限 600），在 web 的 ⚙ 设置里改最方便，也可以手动编辑：

```json
{
  "models": [
    {"id": "a1b2c3d4", "name": "DS 4.1 Flash", "model": "deepseek-v4.1-flash",
     "protocol": "openai", "base_url": "https://your-gateway.example.com/v1", "api_key": "sk-...",
     "reasoning_effort": "low", "context_window_tokens": 128000, "temperature": null,
     "extra_body": {}, "supports_images": true, "visible": true}
  ],
  "default_model": "a1b2c3d4",
  "approval_mode": "ask",
  "busy_input_mode": "interject",
  "max_steps": 50,
  "fold_tool_calls": true,
  "auto_compact": true,
  "compact_threshold_percent": 80,
  "web_search_enabled": true,
  "web_search_provider": "bing",
  "web_search_engine": "search_std",
  "web_fetch_enabled": true,
  "image_gen_enabled": true,
  "image_gen_provider": "zhipu",
  "image_gen_model": null,
  "image_gen_base_url": null,
  "image_search_enabled": true,
  "computer_use_enabled": false,
  "hooks": [],
  "sandbox_enabled": true,
  "sandbox_network": false,
  "allow_outside_workspace": false,
  "skills_enabled": true,
  "skill_dirs": [],
  "disabled_skills": [],
  "memory_enabled": true,
  "workspaces": ["/Users/me/projects/foo"],
  "trusted_dirs": ["/Users/me/data"],
  "ripgrep_path": null,
  "user_name": "me",
  "web_token": "..."
}
```

- `protocol` 是接口协议：`openai`（默认，chat/completions）或 `anthropic`（messages）。不写时按接口地址猜：
  官方 `api.anthropic.com` 认成 anthropic，其余按 OpenAI 兼容
- 思考档位、上下文长度、温度、额外参数、能不能看图都跟着模型走，换模型就一起换。思考档位按接口实测过能用的值来选
  （DeepSeek：关 / 低 / 高 / 最高；智谱：低 / 高 / 最高；OpenRouter：关 / 低 / 中 / 高），OpenRouter 用它自己的
  `reasoning.effort` 写法，其他用顶层 `reasoning_effort`
- `visible: false` 的模型不出现在会话的模型列表里（已经在用它的任务不受影响）
- 每个任务记住自己用的模型和权限，续跑时接着用
- 优先级：命令行参数 > 配置文件 > 环境变量 > 默认值。环境变量（`POCKETAGENT_MODEL` / `POCKETAGENT_BASE_URL` /
  `POCKETAGENT_API_KEY` / `POCKETAGENT_PROTOCOL` 等，完整列表见 `pocketagent/config.py`）只在一个模型都没配时才用
- `-m` 可以写配置里模型的名称、id 或模型名；都对不上就用默认模型的接口、只换模型名
- 任务记录、trace、附件、影子仓库、后台命令、worktree、用量、自动化、工具输出都在 `~/.pocketagent/`（`POCKETAGENT_HOME` 可以改到别处）

## 钩子

在几个固定时机跑你自己的脚本：拦住危险操作、改完文件顺手格式化、给模型补一段上下文、压缩前把结论落盘、
等你审批时推手机、任务结束归档。配置在 `config.json` 的 `hooks` 里，或者在 ⚙ 设置 → 钩子里增删：

```json
{
  "hooks": [
    {"event": "pre_tool_use", "matcher": "bash", "command": "~/bin/guard.sh", "timeout_seconds": 30},
    {"event": "post_tool_use", "matcher": "edit_file|write_file", "command": "npx prettier --write ."},
    {"event": "user_prompt_submit", "command": "~/bin/git-context.sh"},
    {"event": "notification", "command": "~/bin/push-me.sh"},
    {"event": "turn_end", "command": "~/bin/notify.sh", "enabled": false}
  ]
}
```

- `event`，按发生顺序：
  - `session_start`：一个任务的第一轮开始之前（续跑的会话不算）
  - `user_prompt_submit`：用户发来一句话，这一轮开始之前
  - `pre_tool_use`：工具审批通过、真正执行之前
  - `post_tool_use`：工具执行完、结果截断之后
  - `subagent_stop`：子 agent 干完、把结论交回主 agent 之前
  - `pre_compact`：上下文要压缩之前
  - `notification`：agent 要请你审批、或者要你回答问题之前
  - `turn_end`：这一轮结束（答完 / 被拒绝 / 出错 / 达到步数上限）
  - `session_end`：任务结束（删任务、服务退出、命令行退出；进程被强杀时不会跑）
- `matcher`：工具名，支持 `*` 通配，也可以用 `|` 或 `,` 分隔多个（`bash`、`edit_*`、`mcp__*`）；留空表示全部。
  只对 `pre_tool_use` / `post_tool_use` 有意义
- `command`：交给 `bash -c` 的命令，工作目录是任务的工作区；`timeout_seconds` 默认 30 秒，最长 600
- 脚本按你的权限跑：不进沙箱、也不要审批——它本来就是用来看住 agent 的

脚本从 stdin 拿到一份 JSON，公共字段是 `event`、`session_id`、`workspace`：

| 时机 | 另外的字段 |
|---|---|
| `session_start` | （公共字段就够） |
| `user_prompt_submit` | `prompt` |
| `pre_tool_use` | `tool_name`、`tool_input`、`tool_call_id`、`summary` |
| `post_tool_use` | `tool_name`、`tool_input`、`tool_call_id`、`tool_result`、`is_error` |
| `subagent_stop` | `subagent_type`、`description`、`stop_reason`、`result`、`tool_calls`、`seconds` |
| `pre_compact` | `reason`（auto / overflow / manual）、`tokens_before`、`messages` |
| `notification` | `kind`（approval / question）、`tools` 或 `questions`、`summary` |
| `turn_end` | `stop_reason`、`final_text` |
| `session_end` | `turns`、`tool_calls` |

环境变量里有 `POCKETAGENT_EVENT` / `POCKETAGENT_SESSION_ID` / `POCKETAGENT_WORKSPACE` / `POCKETAGENT_HOME`，
工具事件还有 `POCKETAGENT_TOOL_NAME`，简单脚本不用去解析 JSON。

退出码和输出：

- `0`：通过。stdout 可以写 `{"decision": "block", "reason": "…", "additional_context": "…"}`
- `2`：拦住。stderr（没有就用 stdout）是原因
- 其他：钩子自己出错了（超时、脚本报错），只在界面上提示，不影响这一轮

拦住只对三个时机有效：`pre_tool_use` 不执行这次调用，`user_prompt_submit` 不跑这一轮，`session_start` 也是这一轮不跑。
其余时机是「已经发生了，拦不住」：`post_tool_use` 的 `block` 只把原因标成错误，`subagent_stop` / `pre_compact` /
`notification` / `turn_end` / `session_end` 的 `block` 不生效。

`additional_context` 会作为一条提示接在用户消息、工具结果或子 agent 结论后面，模型下一步就看得到
（`session_start` 和 `user_prompt_submit` 接在用户消息后面，`post_tool_use` 接在工具结果后面，`subagent_stop` 接在结论后面）。
`pre_tool_use` 被拦住时，模型拿到的是错误结果，可以换个做法接着做，这一轮不用停。钩子对子 agent 调用的工具同样生效；
一轮被中断时不跑 `turn_end`，免得 Ctrl-C 还要等脚本跑完。

## 模型协议

每个模型单独选协议，⚙ 设置 → 模型里的「接口协议」或配置文件里的 `protocol`：

- **OpenAI 兼容**（默认）：`/v1/chat/completions`，new-api、vLLM、DeepSeek、Qwen 网关等都走这个
- **Anthropic**：`/v1/messages`，接口地址留空就用 `https://api.anthropic.com`；网关上的 Anthropic 入口把
  `base_url` 写到 `/v1` 或 `/messages` 都认。密钥放在 `api_key`，请求头按官方的 `x-api-key` 发
- 工具调用、流式输出、图片、用量（含缓存命中）都会在 provider 里转成 Anthropic 的 content blocks / usage；
  Anthropic 的 thinking 块要求原样带回历史，当前消息格式存不下，所以先不接思考档位（界面上只有「默认」）
