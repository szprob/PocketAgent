"""trace 回放和评测：拿 JSONL trace 重放、对比不同模型或提示词，拿用例批量评测。

- trace_reader.py  把一份 trace 按轮整理成摘要（说了什么、谁答的、步数、工具调用、结果、用量、耗时、起始检查点）
- workspace.py     临时工作区副本：从起始检查点（影子仓库）或者一个目录复制出来，跑完删掉
- runner.py        无人值守地跑一组话：权限怎么收、哪些功能关掉，理由都写在那里
- replay.py        pocketagent replay：重跑 trace 里的话，和原来那次并排对比
- cases.py         评测用例（JSON）的读取和检查（命令退出码、回答里有没有某段文字）
- evaluate.py      pocketagent eval：每个用例 × 每个模型跑一遍
- report.py        对比表、JSON + Markdown 报告（写到 ~/.pocketagent/evals/<时间>/）
"""
