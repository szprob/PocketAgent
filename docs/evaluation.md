# 回放和评测

每个任务都会把事件逐行写进 `~/.pocketagent/traces/<任务 id>.jsonl`。拿这份 trace 可以：

- **回放**（`pocketagent replay`）：把当时用户说的话按顺序重跑一遍，换个模型、换份系统提示词，和原来那次并排对比
- **评测**（`pocketagent eval`）：写几个固定用例（提示词 + 样例目录 + 检查），逐个模型跑，看哪个通过

两者都只在命令行里，都是无人值守地跑，都在**临时副本**里跑，绝不碰你的真实工作区。代码在 `pocketagent/evaluation/`。

## 回放

```bash
pocketagent replay ~/.pocketagent/traces/20261105-090005-1ae382.jsonl          # 用原来的模型再跑一遍
pocketagent replay 20261105-090005-1ae382 -m "DS 4.1 Flash" -m glm-5             # 直接写任务 id；两个模型各跑一遍
pocketagent replay 20261105-090005-1ae382 --system-prompt-file prompt.md        # 换系统提示词
pocketagent replay old.jsonl -C ~/projects/foo                                   # 起始检查点没了：复制这个目录当工作区
```

| 选项 | 说明 |
|---|---|
| `-m` / `--model ID` | 用哪个模型（配置里的 id / 名称 / 模型名，和 `-m` 一样），可以给多次；不给就用 trace 里记的原模型，找不到用默认模型 |
| `--system-prompt-file PATH` | 用文件内容整个换掉系统提示词；文件里写 `{{default_system_prompt}}` 的地方放回默认那份（只想加几句时用） |
| `-C` / `--workspace DIR` | 复制这个目录当工作区（不复制 `.git`）。给了就不用检查点 |
| `--keep-workspaces` | 跑完不删临时副本，路径写进报告，方便看模型改了什么 |
| `-v` / `--verbose` | 把每次运行的回复和工具调用打出来 |

工作区怎么来：

- 默认从 trace 里第一个检查点（「任务开始时」那张影子 git 快照，见[权限与安全](safety.md)）原样取出到系统临时目录。
  快照里只有文件，没有 `.git`；嵌套的 git 仓库在会话记录还在时一起取出来
- 检查点找不到（任务删掉后快照会被 git 清理、检查点当时没开或拍失败、没装 git）就**拒绝回放**，提示用 `--workspace`；
  不会拿你现在的工作区凑合——它可能已经和当时不一样了，更不能在里面直接跑
- 每个模型各用一份新副本，跑完删掉

## 评测

```bash
pocketagent eval examples/eval                      # 目录：读下面所有 *.json（不往子目录找）
pocketagent eval examples/eval/fix-add.json -m one -m two
```

选项和回放一样（`-m`、`--system-prompt-file`、`--keep-workspaces`、`-v`），没有 `--workspace`：工作区是用例自己的样例目录。
全部通过时退出码为 0，否则为 1，可以放进 CI。

用例写成 JSON（一个文件一个用例，或者一个用例数组），示例见 [examples/eval/](../examples/eval/)：

```json
{
  "name": "修复加法",
  "prompt": "calc.py 里的 add 函数算出来的结果不对，帮我修好",
  "fixture": "fixtures/calc",
  "checks": [
    {"type": "command", "command": "python3 -m unittest -q test_calc", "timeout_seconds": 60},
    {"type": "answer_contains", "text": "calc.py"}
  ]
}
```

| 字段 | 说明 |
|---|---|
| `name` | 用例名，不能重复 |
| `prompt` | 一句话；也可以是数组，按顺序一句一轮 |
| `fixture` | 当工作区复制的样例目录，相对用例文件所在目录；不写就从空目录开始 |
| `checks` | 至少一条。`command`：agent 跑完后在副本里用 bash 执行，退出码为 0 才算过（`timeout_seconds` 默认 120）；`answer_contains`：最后一轮的回答里有这段文字 |

通过 = 这一轮正常说完（`done`）而且每条检查都过了。检查命令和 agent 的 bash 用同一套沙箱：副本里的代码可能是 agent 写的，
不能让它借检查命令跑到沙箱外面；没有沙箱的机器上检查命令直接执行，开头会提醒。

## 无人值守怎么审批

没人在场点确认，所以回放和评测都按这套跑（`pocketagent/evaluation/runner.py`）：

- 审批模式 `auto_edit`：改文件直接放行——工作区就是副本，改的只能是副本
- 其余要审批的：只放行**沙箱里**的 bash（沙箱只让它写副本、临时目录和缓存目录）；碰工作区以外的路径、要跳出沙箱、碰密钥、
  MCP 工具一律拒绝。和 `-p` 一样，被拒绝时这一轮就停下，对比表里记为失败（`denied`）
- 机器上没有沙箱（Windows、没装 bubblewrap 的 Linux）时 bash 也拒绝：命令没有隔离，可能改到副本以外
- 不用 AI 审批（自动化用的那档）：审批模型的判断有随机性、还多花钱，会让模型之间的对比不干净
- 关掉：免问目录、「工作目录外不再询问」（都会把副本以外的目录当工作区）、钩子、MCP、computer use、项目记忆、检查点；
  子 agent 不另开 worktree，也不用设置里单独选的模型——子 agent 也用被测的模型，对比才公平
- 提问（`ask_user`）没人回答；计划模式的方案不会被批准

## 对比表和报告

终端里打一张并排的表，每次运行一列（回放的第一列是原来那次）：

```
                  原始                    DS Flash（deepseek-flash）  GLM（glm-x）
----------------  ----------------------  --------------------------  --------------
模型              deepseek-flash          deepseek-flash              glm-x
结果              成功                    成功                        失败（denied）
步数              1                       3                           1
工具调用          0                       2                           1
出错              0                       0                           0
被拒绝            0                       0                           1
token（入 / 出）  1,234 / 56              900 / 30                    0 / 0
费用              -                       $0.0012                     -
耗时（秒）        8.5                     3.1                         0.4
最终回答          notes.txt 里是「原始…   看过了，notes.txt 里是…
```

- 步数是调了几次模型；出错 = 工具执行出错 + 这一轮报的错（接口失败等），被拒绝的调用单独算
- 费用只有接口报了才有（OpenRouter 等），没有价目表，不估
- 评测每个用例一张表，多出每条检查的结果，最后一张汇总表是每个模型的通过数

同时写 `~/.pocketagent/evals/<时间>/report.json` 和 `report.md`（不进仓库）：JSON 里有每次运行每一轮的完整摘要
（用户的话、模型、步数、每个工具调用的结果、用量、耗时、完整的最终回答）和新 trace 的路径。
每次运行照常写自己的 trace，会话文件跑完删掉，任务列表里不会多出一堆回放出来的任务。

## 不一样的地方

回放尽量还原当时，但这些做不到，开头会提醒：

- 只重放文字：带的附件、只有附件没有文字的轮次、中途的插话都不重放
- 工作区路径变了（是临时目录），提示词里写死的绝对路径会指到副本外面，被拒绝
- 各轮之间你手动改过的文件、当时的项目记忆、MCP 工具都没有
- 模型本身有随机性：同一个模型给两次（`-m one -m one`）可以看波动

## 读 trace

`pocketagent/evaluation/trace_reader.py` 的 `read_trace(path)` 把 trace 按轮整理成摘要，可以自己写脚本用：
每轮的用户原话、用过的模型、步数、工具调用（名字 + ok / error / denied / missing）、报错、最终回答、token、费用、耗时、
起始检查点。进程被杀时最后一行可能只写了一半，读的时候跳过坏行并记数；没有 `turn_end` 的一轮记为 `incomplete`。
`turn_start` 里记着工作区和模型配置、`llm_response` 里记着这一步的模型；更早的 trace 没有这些字段，模型显示为「未记录」。
