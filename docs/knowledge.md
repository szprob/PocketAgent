# 技能与项目记忆

## 技能

按 [Agent Skills](https://agentskills.io) 开放标准，和 Claude Code、Codex 等用的是同一种格式：一个目录一个技能，
目录里的 `SKILL.md` 开头写 `name` 和 `description`，正文写怎么做，旁边可以放 `scripts/`、`references/`、`assets/`。

- 系统提示里放 `<available_skills>` 清单（名字、描述、SKILL.md 的位置），模型用得上时调 `activate_skill`
  拿到正文（`<skill_content>` 包着，附带技能目录和文件清单），也可以直接 read_file；同一个技能不重复加载
- 消息开头写 `/技能名` 直接启用（命令行里 `/skills` 列出全部）
- 压缩上下文时，加载过的技能正文原样保留，不进摘要
- 不合规范（名字不是小写短横线、和目录名不一致、描述超过 2048 字）只提示、照样加载；没有描述的跳过

找技能的地方，项目级优先于用户级；指向同一个真实目录的（软链）只算一次：

- 工作目录下的 `.pocketagent/skills/`、`.agents/skills/`、`.claude/skills/`
- `~/.pocketagent/skills/`、`~/.agents/skills/`、`~/.claude/skills/`
- ⚙ 设置 → 技能里额外加的目录

已经在别的 agent 里攒了一套技能的话，把 `~/.pocketagent/skills` 软链过去就行。
⚙ 设置 → 技能能看到找到了哪些、点开看正文、不合规范的提示，可以单个关掉。

## 项目记忆

每个工作区一份跨会话的笔记，放在 `~/.pocketagent/projects/<工作区路径>/memory/`，不进仓库。格式和 Claude Code 的 auto memory 一样：
`MEMORY.md` 是索引（一行一条），每条记忆一个文件，开头写 `name` / `description` / `type`（user / feedback / project / reference）。

- 开任务时把 `MEMORY.md` 放进系统提示，某条用到时 agent 再去读
- 你说「记住……」它就记；它觉得以后用得上也会自己记，记了会说一声。读写记忆目录不用确认（只读模式除外）
- ⚙ 设置 → 记忆：按工作区看每条记忆、点开看内容、删除（索引里那行一起去掉），也可以整个关掉
