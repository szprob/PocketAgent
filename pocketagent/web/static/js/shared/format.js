// 不碰 DOM 的格式化小函数：数字、字节、时间、路径。两个前端共用，Node 里也能直接测。

const COMPACT_UNITS = [
  { size: 1_000_000_000, suffix: "b" },
  { size: 1_000_000, suffix: "m" },
  { size: 1000, suffix: "k" },
];

// 大数字缩成 1.2k / 35k / 1.5m / 2.1b；拿不到数（后端没给）显示 ?，别冒充 0
export function formatTokens(count) {
  const number = count === null || count === undefined || count === "" ? NaN : Number(count);
  if (!Number.isFinite(number)) {
    return "?";
  }
  for (const unit of COMPACT_UNITS) {
    if (number >= unit.size) {
      const value = number / unit.size;
      // 十以内留一位小数（1.2k），再大就取整（35k），不然数字太长
      return `${value.toFixed(value >= 10 ? 0 : 1)}${unit.suffix}`;
    }
  }
  return String(Math.round(number));
}

export function formatNumber(count) {
  return (Number(count) || 0).toLocaleString("zh-CN");
}

export function formatBytes(size) {
  const value = Number(size) || 0;
  if (value >= 1024 * 1024) {
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }
  if (value >= 1024) {
    return `${Math.round(value / 1024)} KB`;
  }
  return `${value} B`;
}

function parseDate(isoText) {
  if (!isoText) {
    return null;
  }
  const date = new Date(isoText);
  return Number.isNaN(date.getTime()) ? null : date;
}

function isToday(date) {
  return date.toDateString() === new Date().toDateString();
}

// 今天的只显示时分，别的天带上月-日
export function formatClock(isoText) {
  const date = parseDate(isoText);
  if (!date) {
    return "";
  }
  const time = date.toTimeString().slice(0, 5);
  if (isToday(date)) {
    return time;
  }
  return `${date.getMonth() + 1}-${date.getDate()} ${time}`;
}

// 一周以内说「几分钟前 / 几天前」，再早就直接给日期，「38 天前」不如日期好认
export function formatRelativeTime(isoText) {
  const date = parseDate(isoText);
  if (!date) {
    return "";
  }
  // 两台机器时钟差一点时可能是负数，按「刚刚」算
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 60) {
    return "刚刚";
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} 分钟前`;
  }
  if (seconds < 86400) {
    return `${Math.floor(seconds / 3600)} 小时前`;
  }
  if (seconds < 86400 * 7) {
    return `${Math.floor(seconds / 86400)} 天前`;
  }
  return formatClock(isoText);
}

// 路径的最后一段；Windows 路径用反斜杠，也要认
export function workspaceName(path) {
  if (!path) {
    return "";
  }
  const parts = String(path).split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path);
}

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "avif", "ico"]);

export function isImagePath(path) {
  const name = workspaceName(path);
  const dot = name.lastIndexOf(".");
  return dot >= 0 && IMAGE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}
