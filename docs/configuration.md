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

## 模型协议

每个模型单独选协议，⚙ 设置 → 模型里的「接口协议」或配置文件里的 `protocol`：

- **OpenAI 兼容**（默认）：`/v1/chat/completions`，new-api、vLLM、DeepSeek、Qwen 网关等都走这个
- **Anthropic**：`/v1/messages`，接口地址留空就用 `https://api.anthropic.com`；网关上的 Anthropic 入口把
  `base_url` 写到 `/v1` 或 `/messages` 都认。密钥放在 `api_key`，请求头按官方的 `x-api-key` 发
- 工具调用、流式输出、图片、用量（含缓存命中）都会在 provider 里转成 Anthropic 的 content blocks / usage；
  Anthropic 的 thinking 块要求原样带回历史，当前消息格式存不下，所以先不接思考档位（界面上只有「默认」）
