"""Markdown 文件开头 --- 包着的元数据（技能的 SKILL.md、记忆文件都用它）。

只认最常见的 YAML 写法：key: value、带引号的值、> 和 | 多行块；嵌套的 map 会被拼成一行字符串，不报错。
比严格的 YAML 宽松：`description: 用户说: 帮我…` 这种值里带冒号的也能读（Agent Skills 规范建议客户端这样兼容）。
"""

import re

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出开头 --- 包着的元数据和正文。只认最常见的 YAML 写法：key: value、带引号的值、> 和 | 多行块。"""
    match = _FRONTMATTER.match(text)
    if match is None:
        return {}, text
    return _parse_simple_yaml(match.group(1)), text[match.end():]


def _parse_simple_yaml(block: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        key_match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if key_match is None:
            continue
        key, value = key_match.group(1), key_match.group(2).strip()
        if value[:1] in (">", "|"):
            continuation, index = _indented_block(lines, index)
            separator = "\n" if value.startswith("|") else " "
            metadata[key] = separator.join(continuation).strip()
        else:
            continuation, index = _indented_block(lines, index)
            metadata[key] = _unquote(" ".join([value, *continuation]).strip())
    return metadata


def _indented_block(lines: list[str], index: int) -> tuple[list[str], int]:
    """读紧跟着的缩进行（多行值）。"""
    collected = []
    while index < len(lines) and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
        collected.append(lines[index].strip())
        index += 1
    return collected, index


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value
