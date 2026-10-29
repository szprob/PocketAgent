"""HTTP + WebSocket 后端：web、桌面端等所有前端共用，只走下面的 REST 和每个任务一条 WebSocket。

代码分布：app.py 组装和启动（鉴权、静态页面）；routes/ 按资源分的接口；session.py 一个任务的运行时（WebSession）；
manager.py 所有任务、工作区、配置和自动化调度（SessionManager）；history.py 快照里的历史；files.py 文件树和预览。

前端 → 服务（WebSocket，JSON）：
  {"type": "user_message", "text": ..., "attachment_ids": [...]}   空闲就开一轮；忙着就按设置插话或排队
  {"type": "approval", "request_id": ..., "answer": "allow_once" | "allow_always" | "allow_always_parents" | "deny"}
  {"type": "approval", "request_id": ..., "answers": [...]}   合并的审批卡（approval_request 带 items）每项一个答复
  {"type": "question_answer", "request_id": ..., "answers": [{"selected": [...], "other": "..."}, ...] | null（跳过）}
  {"type": "interrupt"} / {"type": "compact"} / {"type": "remove_queued", "index": n}
  {"type": "set_mode", "mode": ...} / {"type": "set_model", "model_id": ...}
服务 → 前端：
  snapshot（连上时的完整历史；用户条目带 message_id，rewind 用它指定恢复到哪句话）、
  state（是否在跑、模型、审批模式、上下文占用、排队和插话）、
  event（agent 事件，见 core/events.py）、approval_request / approval_resolved、question_request / question_resolved、
  （approval_request 一项时字段平铺；一批里有几项要问时合成一张，字段放在 items 列表里）
  returned_inputs（中断时退回的输入）、error

REST（都在 /api 下）：
  info                                   工作区、模型（含可选思考档位、上下文长度）、审批模式、用户名、斜杠命令表
  command            POST                斜杠命令（web 和命令行共用一套，见 interaction/commands.py）
  models/{id}         PATCH              选模型面板里改思考档位、上下文长度（存进配置，马上生效）
  workspaces          POST / DELETE      加 / 移除工作区
  workspaces/default  POST               把某个工作区设成新任务的默认
  files / file / file/raw                工作区文件树 / 文件预览 / 原始文件（图片预览）
  browse                                 选工作区时浏览本机目录（只列目录）
  sessions            GET / POST         任务列表 / 新建任务（工作区、模型、审批模式；ephemeral=true 用临时目录；要不要独立 worktree 按设置自动决定）
  sessions/{id}       GET / PATCH / DELETE   快照 / 改标题 / 删除
  sessions/{id}/rewind                   POST 删掉某条用户消息和之后的全部对话，把这句话放回输入框
  sessions/{id}/overview                 右侧栏概要（用量、缓存命中、最新回复），比快照轻
  sessions/{id}/attachments              POST 上传（multipart），GET .../{附件 id} 取文件
  sessions/{id}/file/raw                 GET 会话工作区里的原始文件（聊天和产出里的图片）
  sessions/{id}/changes                  GET 文件变更 + 检查点（影子 git），GET .../diff?path=，POST .../revert
  sessions/{id}/changes/turn              GET 本轮开始到现在改了什么（聊天里贴本轮图片）
  sessions/{id}/checkpoints/restore      POST 把整个工作区恢复到某个检查点
  usage / usage/balance                  本机 token 用量 / 各家账户余额
  config              GET / PUT          设置；config/test 测试模型连接
  automations         GET / POST         自动化列表 / 新建；automations/{id} PUT / DELETE；.../run 立即运行

页面里所有地址都是相对路径，挂在网关子路径下（如 /web-xxx/pocketagent/）也能用。
"""
