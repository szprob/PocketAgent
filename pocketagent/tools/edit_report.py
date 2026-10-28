"""改完文件以后给模型看的回执：改动处前后几行（带行号）、加减了几行，以及进程内的语法检查。

模型拿到回执就知道改到了哪里、改完长什么样，不用再 read_file 确认一遍。
语法检查只做标准库能做的（.py 用 ast、.json 用 json、.toml 用 tomllib），不调外部 linter；
查出来的问题只当提示附在结果后面，不标错误：文件已经写进去了，标错误模型会以为没改成又改一遍。
"""

import ast
import difflib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONTEXT_LINES = 3
# 片段总长的上限：改动很分散时只列前面几处，剩下的让模型自己 read_file
MAX_SNIPPET_CHARS = 4000
MAX_LINE_CHARS = 300


@dataclass
class LineRange:
    """改完后的文件里，第 start 到 end 行（含，从 1 数）有改动；纯删除时 start == end，指向删除处。"""

    start: int
    end: int


@dataclass
class ChangeSummary:
    added_lines: int
    removed_lines: int
    ranges: list[LineRange]


def summarize_changes(old_text: str, new_text: str) -> ChangeSummary:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    added = 0
    removed = 0
    ranges: list[LineRange] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        added += new_end - new_start
        removed += old_end - old_start
        # 新文件里的行号从 1 数；纯删除没有新行，就指向删除位置那一行
        start = new_start + 1
        end = max(new_end, start)
        ranges.append(LineRange(start=start, end=end))
    return ChangeSummary(added_lines=added, removed_lines=removed, ranges=ranges)


def numbered_snippets(new_text: str, ranges: list[LineRange]) -> str:
    """每处改动前后各带 CONTEXT_LINES 行，重叠的窗口合并，段与段之间用「…」隔开。"""
    lines = new_text.splitlines()
    if not lines or not ranges:
        return ""
    windows = _merged_windows(ranges, len(lines))
    parts: list[str] = []
    used_chars = 0
    for index, window in enumerate(windows):
        block = _numbered_block(lines, window)
        if parts and used_chars + len(block) > MAX_SNIPPET_CHARS:
            parts.append(f"…（还有 {len(windows) - index} 处改动没列出，需要的话用 read_file 看）")
            break
        parts.append(block)
        used_chars += len(block)
    return "\n…\n".join(parts)


def _merged_windows(ranges: list[LineRange], line_count: int) -> list[LineRange]:
    windows: list[LineRange] = []
    for change in ranges:
        start = max(1, change.start - CONTEXT_LINES)
        end = min(line_count, change.end + CONTEXT_LINES)
        if windows and start <= windows[-1].end + 1:
            windows[-1].end = max(windows[-1].end, end)
        else:
            windows.append(LineRange(start=start, end=end))
    return windows


def _numbered_block(lines: list[str], window: LineRange) -> str:
    numbered = []
    for line_number in range(window.start, window.end + 1):
        text = lines[line_number - 1]
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "…"
        numbered.append(f"{line_number:6d}\t{text}")
    return "\n".join(numbered)


def syntax_problem(path: Path, text: str) -> str | None:
    """按扩展名用标准库检查语法；没问题或者不认识这种文件返回 None。"""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _python_problem(text)
    if suffix == ".json":
        return _json_problem(text)
    if suffix == ".toml":
        return _toml_problem(text)
    return None


def _python_problem(text: str) -> str | None:
    # 用 ast.parse 而不是 py_compile：后者会在工作区里写 .pyc
    try:
        ast.parse(text)
    except SyntaxError as error:
        return f"第 {error.lineno} 行：{error.msg}"
    return None


def _json_problem(text: str) -> str | None:
    try:
        json.loads(text)
    except json.JSONDecodeError as error:
        return f"第 {error.lineno} 行第 {error.colno} 列：{error.msg}"
    return None


def _toml_problem(text: str) -> str | None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return str(error)
    return None


def syntax_hint(display_path: str, problem: str) -> str:
    return (
        f"注意：改完后 {display_path} 有语法错误（{problem}）。"
        "如果是分步修改、后面还要接着改，这个中间状态可以先不管；改完了还报错再修。"
    )
