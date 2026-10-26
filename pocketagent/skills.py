"""技能（skills），按 Agent Skills 开放标准（agentskills.io）来：Claude Code、Codex 等用的同一种格式，
所以 ~/.claude/skills、~/.agents/skills 里现成的技能可以直接用。

    <技能目录>/<技能名>/
      SKILL.md        必须有：开头 --- 里写 name、description，正文写怎么做
      scripts/        可选：脚本
      references/     可选：参考文档，用到再读
      assets/         可选：模板、数据

渐进加载：系统提示里只放 <available_skills> 清单（名字、描述、SKILL.md 位置）；模型判断用得上时调 activate_skill
（或者直接 read_file 读 SKILL.md）拿到正文；正文里提到的脚本、参考文件再用 read_file / bash 去用。
用户也可以在消息开头写 /技能名 直接启用。加载过的技能正文在压缩上下文时原样保留。

找技能的地方，项目级优先于用户级，同一级里前面的优先：
- 工作目录下的 .pocketagent/skills、.agents/skills、.claude/skills
- ~/.pocketagent/skills、~/.agents/skills、~/.claude/skills
- 设置里额外加的目录
规范里的硬性要求（名字只能小写字母、数字、短横线，要和目录名一致，描述不超过 1024 字）不满足时只记一条提示、照样加载；
没有描述的技能模型没法判断什么时候用，跳过。
"""

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocketagent.frontmatter import split_frontmatter

SKILL_FILE_NAME = "SKILL.md"
PROJECT_SKILL_DIRS = [".pocketagent/skills", ".agents/skills", ".claude/skills"]
USER_SKILL_DIRS = ["~/.agents/skills", "~/.claude/skills"]
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_LIMIT_CHARS = 64
DESCRIPTION_LIMIT_CHARS = 1024
# 系统提示里的技能清单最多这么长，超出的技能只列名字和位置
LISTING_LIMIT_CHARS = 12000
# 启用技能时列出的附带文件最多这么多个
RESOURCE_LIST_LIMIT = 50
SKIPPED_RESOURCE_PARTS = {".git", "__pycache__", "node_modules", ".DS_Store"}
SKILL_CONTENT_PATTERN = re.compile(r'<skill_content name="([^"]+)">.*?</skill_content>', re.DOTALL)


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
    # 不符合规范的地方（只提示，不影响使用）
    warnings: list[str] = field(default_factory=list)

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
            "warnings": self.warnings,
        }


@dataclass
class SkillCatalog:
    skills: list[Skill] = field(default_factory=list)
    # 没加载上的技能、重名被覆盖的技能，给设置页看
    problems: list[str] = field(default_factory=list)

    def find(self, name: str) -> Skill | None:
        return next((skill for skill in self.skills if skill.name == name), None)


def skill_directories(workspace: Path | None, home_dir: Path, extra_dirs: list[str]) -> list[SkillDirectory]:
    """workspace 为 None 时只看用户级和自定义的目录。指向同一个真实目录的（软链）只留第一个。"""
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


def discover_skills(directories: list[SkillDirectory]) -> SkillCatalog:
    """只看每个目录下一层的 <技能名>/SKILL.md；名字重复时前面的目录优先。"""
    catalog = SkillCatalog()
    for directory in directories:
        if not directory.path.is_dir():
            continue
        for skill_file in sorted(directory.path.glob(f"*/{SKILL_FILE_NAME}")):
            skill = read_skill(skill_file, directory.source, catalog.problems)
            if skill is None:
                continue
            existing = catalog.find(skill.name)
            if existing is not None:
                catalog.problems.append(f"{skill.path}：和 {existing.path} 重名，用前面那个")
                continue
            catalog.skills.append(skill)
    return catalog


def read_skill(skill_file: Path, source: str, problems: list[str]) -> Skill | None:
    try:
        text = skill_file.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        problems.append(f"{skill_file}：读不了（{error}）")
        return None
    metadata, _ = split_frontmatter(text)
    folder_name = skill_file.parent.name
    name = str(metadata.get("name") or folder_name).strip()
    description = " ".join(str(metadata.get("description") or "").split())
    if not description:
        problems.append(f"{skill_file}：没有 description，模型没法判断什么时候用，跳过")
        return None
    return Skill(
        name=name,
        description=description,
        path=skill_file,
        source=source,
        warnings=_spec_warnings(metadata, name, folder_name, description),
    )


def _spec_warnings(metadata: dict[str, str], name: str, folder_name: str, description: str) -> list[str]:
    warnings = []
    if "name" not in metadata:
        warnings.append("没写 name，用了目录名")
    if not NAME_PATTERN.match(name) or len(name) > NAME_LIMIT_CHARS:
        warnings.append(f"name 应该是不超过 {NAME_LIMIT_CHARS} 个字符的小写字母、数字和短横线")
    if name != folder_name:
        warnings.append(f"name（{name}）和目录名（{folder_name}）不一致")
    if len(description) > DESCRIPTION_LIMIT_CHARS:
        warnings.append(f"description 有 {len(description)} 字，规范上限 {DESCRIPTION_LIMIT_CHARS}，清单里会截断")
    return warnings


# ---- 给模型看的 ----


def skills_prompt_section(skills: list[Skill]) -> str:
    """系统提示里的技能清单，用规范推荐的 <available_skills> 写法。"""
    if not skills:
        return ""
    entries = []
    used_chars = 0
    for skill in skills:
        description = skill.description
        if len(description) > DESCRIPTION_LIMIT_CHARS:
            description = description[:DESCRIPTION_LIMIT_CHARS] + "…"
        entry = _catalog_entry(skill, description)
        if used_chars + len(entry) > LISTING_LIMIT_CHARS:
            entry = _catalog_entry(skill, "")
        used_chars += len(entry)
        entries.append(entry)
    instructions = (
        "下面是可用的技能。任务和某个技能的描述对得上时，先调 activate_skill 加载它的完整说明"
        "（也可以用 read_file 读 location 处的 SKILL.md），再照着做。"
        "技能里提到的相对路径都相对技能目录（SKILL.md 所在的目录），调工具时换成绝对路径。"
    )
    return f"{instructions}\n\n<available_skills>\n" + "\n".join(entries) + "\n</available_skills>"


def _catalog_entry(skill: Skill, description: str) -> str:
    lines = ["  <skill>", f"    <name>{html.escape(skill.name)}</name>"]
    if description:
        lines.append(f"    <description>{html.escape(description, quote=False)}</description>")
    lines += [f"    <location>{html.escape(str(skill.path), quote=False)}</location>", "  </skill>"]
    return "\n".join(lines)


def skill_content(skill: Skill) -> str:
    """启用技能时交给模型的内容：正文（去掉开头的元数据）+ 技能目录 + 附带文件清单（只列不读）。"""
    text = skill.path.read_text(encoding="utf-8", errors="replace")
    _, body = split_frontmatter(text)
    resources = _resource_files(skill)
    lines = [
        f'<skill_content name="{html.escape(skill.name)}">',
        body.strip(),
        "",
        f"技能目录：{skill.directory}",
        "这个技能里的相对路径都相对技能目录。",
    ]
    if resources:
        lines.append("<skill_resources>")
        lines += [f"  <file>{html.escape(item, quote=False)}</file>" for item in resources]
        lines.append("</skill_resources>")
    lines.append("</skill_content>")
    return "\n".join(lines)


def _resource_files(skill: Skill) -> list[str]:
    files = []
    for path in sorted(skill.directory.rglob("*")):
        relative = path.relative_to(skill.directory)
        if not path.is_file() or path == skill.path or SKIPPED_RESOURCE_PARTS & set(relative.parts):
            continue
        if len(files) >= RESOURCE_LIST_LIMIT:
            files.append(f"……（只列前 {RESOURCE_LIST_LIMIT} 个，完整的用 glob 看）")
            break
        files.append(relative.as_posix())
    return files


def find_skill_command(text: str, skills: list[Skill]) -> tuple[Skill, str] | None:
    """用户消息以 /技能名 开头时，返回 (技能, 后面的话)。"""
    match = re.match(r"^/([A-Za-z0-9_.:-]+)(?:\s+|$)(.*)", text.strip(), re.DOTALL)
    if match is None:
        return None
    skill = next((each for each in skills if each.name == match.group(1)), None)
    return (skill, match.group(2).strip()) if skill else None


def loaded_skill_names(messages: list[dict[str, Any]]) -> set[str]:
    """对话里已经有正文的技能，不用重复加载。"""
    return {match.group(1) for text in _message_texts(messages) for match in SKILL_CONTENT_PATTERN.finditer(text)}


def loaded_skill_blocks(messages: list[dict[str, Any]]) -> list[str]:
    """压缩时要原样保留的技能正文，同名的只留最后一份。"""
    blocks: dict[str, str] = {}
    for text in _message_texts(messages):
        for match in SKILL_CONTENT_PATTERN.finditer(text):
            blocks.pop(match.group(1), None)
            blocks[match.group(1)] = match.group(0)
    return list(blocks.values())


def _message_texts(messages: list[dict[str, Any]]) -> list[str]:
    texts = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [part.get("text", "") for part in content if part.get("type") == "text"]
    return texts
