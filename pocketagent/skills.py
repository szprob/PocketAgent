"""技能（skills）：一个目录一个技能，目录里的 SKILL.md 写着什么时候用、怎么用，旁边可以放脚本和参考资料。

格式和 Claude Code 一样，所以 ~/.claude/skills 里现成的技能可以直接用：

    <技能目录>/<技能名>/SKILL.md
    ---
    name: 技能名
    description: 一段话，说清楚这个技能干什么、什么时候该用
    ---
    正文：具体步骤、命令、注意事项……

系统提示里只列出每个技能的名字和描述（省 token）；模型判断用得上时调 load_skill 读正文，
正文里提到的脚本、参考文件再用 read_file / bash 去用。

找技能的地方，前面的同名技能优先：
- 工作目录下的 .pocketagent/skills、.claude/skills（项目自带的技能）
- ~/.pocketagent/skills、~/.claude/skills（用户自己的技能）
- 设置里额外加的目录
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_FILE_NAME = "SKILL.md"
PROJECT_SKILL_DIRS = [".pocketagent/skills", ".claude/skills"]
USER_SKILL_DIRS = ["~/.claude/skills"]
# 系统提示里每个技能的描述最多这么长，全部技能加起来最多这么长，超出的只列名字
DESCRIPTION_LIMIT_CHARS = 300
LISTING_LIMIT_CHARS = 8000
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


@dataclass
class SkillDirectory:
    path: Path
    # 界面上显示的来源：项目 / 用户 / 自定义
    source: str


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str

    @property
    def directory(self) -> Path:
        return self.path.parent

    def public_dict(self, disabled: bool = False) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "source": self.source,
            "enabled": not disabled,
        }


def skill_directories(workspace: Path | None, home_dir: Path, extra_dirs: list[str]) -> list[SkillDirectory]:
    """workspace 为 None 时只看用户级和自定义的目录。"""
    directories = []
    if workspace is not None:
        directories += [SkillDirectory(workspace / relative, "项目") for relative in PROJECT_SKILL_DIRS]
    directories.append(SkillDirectory(home_dir / "skills", "用户"))
    directories += [SkillDirectory(Path(item).expanduser(), "用户") for item in USER_SKILL_DIRS]
    directories += [SkillDirectory(Path(item).expanduser(), "自定义") for item in extra_dirs]
    unique: list[SkillDirectory] = []
    seen: set[Path] = set()
    for directory in directories:
        real_path = directory.path.resolve()
        if real_path not in seen:
            seen.add(real_path)
            unique.append(directory)
    return unique


def discover_skills(directories: list[SkillDirectory]) -> list[Skill]:
    """只看每个目录下一层的 <技能名>/SKILL.md；名字重复时前面的目录优先。"""
    skills: list[Skill] = []
    seen_names: set[str] = set()
    for directory in directories:
        if not directory.path.is_dir():
            continue
        for skill_file in sorted(directory.path.glob(f"*/{SKILL_FILE_NAME}")):
            skill = read_skill(skill_file, directory.source)
            if skill is None or skill.name in seen_names:
                continue
            seen_names.add(skill.name)
            skills.append(skill)
    return skills


def read_skill(skill_file: Path, source: str) -> Skill | None:
    try:
        text = skill_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    metadata, _ = split_frontmatter(text)
    name = str(metadata.get("name") or skill_file.parent.name).strip()
    description = " ".join(str(metadata.get("description") or "").split())
    if not name:
        return None
    return Skill(name=name, description=description, path=skill_file, source=source)


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


def skills_prompt_section(skills: list[Skill]) -> str:
    """系统提示里的技能清单。"""
    if not skills:
        return ""
    lines = [
        "可用技能（任务和某个技能的描述对得上时，先用 load_skill 读它的完整说明，再照着做）：",
    ]
    used_chars = 0
    for skill in skills:
        description = skill.description
        if len(description) > DESCRIPTION_LIMIT_CHARS:
            description = description[:DESCRIPTION_LIMIT_CHARS] + "…"
        line = f"- {skill.name}：{description}" if description else f"- {skill.name}"
        if used_chars + len(line) > LISTING_LIMIT_CHARS:
            line = f"- {skill.name}"
        used_chars += len(line)
        lines.append(line)
    return "\n".join(lines)
