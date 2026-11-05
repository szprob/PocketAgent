// 后端枚举值对应的中文名。两个前端显示同一个东西要用同一个词，所以放这里。

export const MODE_LABELS = {
  ask: "每次询问",
  ai_auto: "AI 自动审批",
  full_auto: "完全访问",
  read_only: "只读",
  auto_edit: "自动改文件",
};

// 自动化上一轮的结局（对应后端会话的 finish 状态）
export const RUN_STATUS_LABELS = {
  running: "运行中",
  done: "完成",
  error: "出错",
  denied: "有操作被拒绝",
  step_limit: "达到步数上限",
  interrupted: "被中断",
  truncated: "回复超长截断",
  filtered: "被内容审核中断",
  incomplete: "输出中断",
};
