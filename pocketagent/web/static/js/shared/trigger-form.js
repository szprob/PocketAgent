// 触发器的表单值 ↔ 后端 JSON。
//
// 纯函数，不碰 DOM，也不读 state：弹窗只负责把控件里的值收成一个对象交给这里，
// 由这里决定发什么给后端。后端的校验在 pocketagent/server/triggers/model.py，
// 两边的字段名必须一模一样——改这里的时候要顺手看一眼那个文件。

export const TRIGGER_KINDS = ["clock", "task_end", "webhook", "poll"];
export const CONDITION_KINDS = ["always", "status", "match", "shell"];

const ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];
const DEFAULT_DAILY_TIME = "09:00";
const DEFAULT_CLOCK_INTERVAL = 60;
const DEFAULT_POLL_INTERVAL = 5;

export function emptyTrigger() {
  return {
    source: { kind: "clock", mode: "daily", daily_time: DEFAULT_DAILY_TIME, weekdays: ALL_WEEKDAYS },
    condition: { kind: "always" },
  };
}

// 已有触发器 → 弹窗要显示的值。后端给什么都得能开得起来，所以每个字段都有兜底。
export function formValuesOf(trigger) {
  const source = trigger?.source || {};
  const condition = trigger?.condition || {};
  return {
    triggerKind: pick(source.kind, TRIGGER_KINDS, "clock"),
    clockMode: source.mode === "interval" ? "interval" : "daily",
    dailyTime: source.daily_time || DEFAULT_DAILY_TIME,
    clockIntervalMinutes: source.mode === "interval" ? source.interval_minutes || DEFAULT_CLOCK_INTERVAL : DEFAULT_CLOCK_INTERVAL,
    weekdays: Array.isArray(source.weekdays) && source.weekdays.length > 0 ? source.weekdays : ALL_WEEKDAYS,
    watchAutomation: source.automation_id || "",
    pollIntervalMinutes: source.kind === "poll" ? source.interval_minutes || DEFAULT_POLL_INTERVAL : DEFAULT_POLL_INTERVAL,
    pollCommand: source.kind === "poll" ? source.command || "" : "",
    conditionKind: pick(condition.kind, CONDITION_KINDS, "always"),
    conditionPattern: condition.pattern || "",
    conditionCommand: condition.command || "",
  };
}

// 弹窗里的值 → 后端认的触发器。跟当前触发方式无关的字段一律不带上，
// 省得换过触发方式之后旧字段还留在 JSON 里误导人。
export function triggerFromForm(values) {
  return { source: sourceFromForm(values), condition: conditionFromForm(values) };
}

export function sourceFromForm(values) {
  const kind = pick(values.triggerKind, TRIGGER_KINDS, "clock");
  if (kind === "clock") {
    if (values.clockMode === "interval") {
      return { kind, mode: "interval", interval_minutes: positive(values.clockIntervalMinutes, DEFAULT_CLOCK_INTERVAL) };
    }
    return { kind, mode: "daily", daily_time: values.dailyTime || DEFAULT_DAILY_TIME, weekdays: values.weekdays || ALL_WEEKDAYS };
  }
  if (kind === "task_end") {
    return { kind, automation_id: values.watchAutomation || "" };
  }
  if (kind === "poll") {
    return {
      kind,
      interval_minutes: positive(values.pollIntervalMinutes, DEFAULT_POLL_INTERVAL),
      command: values.pollCommand || "",
    };
  }
  // webhook 的密钥在服务端生成：编辑时这里不带 secret，服务端会沿用原来的
  return { kind };
}

export function conditionFromForm(values) {
  const kind = pick(values.conditionKind, CONDITION_KINDS, "always");
  if (kind === "match") {
    return { kind, pattern: values.conditionPattern || "" };
  }
  if (kind === "shell") {
    return { kind, command: values.conditionCommand || "" };
  }
  return { kind };
}

// 哪些字段属于当前这个触发方式：弹窗按它显示 / 隐藏，测试按它检查「没带多余字段」
export function visibleFields(triggerKind) {
  return {
    clock: triggerKind === "clock",
    watch: triggerKind === "task_end",
    poll: triggerKind === "poll",
    webhook: triggerKind === "webhook",
  };
}

function pick(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

function positive(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}
