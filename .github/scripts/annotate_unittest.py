"""把 unittest 输出里失败的用例挂成 GitHub Actions 注解。

Actions 的日志要登录才能看，注解在公开仓库的运行页面上谁都看得到：没装 gh、没登录的时候，
至少能知道是哪条用例挂的、报的什么错。用法：python annotate_unittest.py 日志文件...
"""

import re
import sys
from pathlib import Path

# GitHub 每一步最多显示 10 条 error 注解，一条里多了也看不全
MAX_ANNOTATIONS = 10
MAX_LINES_PER_FAILURE = 25
SEPARATOR = re.compile(r"^(=+|-+)$")
HEADER = re.compile(r"^(FAIL|ERROR): (.+)$")


def failures(log_text: str) -> list[tuple[str, list[str]]]:
    """[(「FAIL: 用例名」, 报错的行), ...]：unittest 每个失败块是 ===== / 标题 / ----- / 回溯。"""
    found: list[tuple[str, list[str]]] = []
    lines = log_text.splitlines()
    index = 0
    while index < len(lines):
        header = HEADER.match(lines[index])
        if not header:
            index += 1
            continue
        title = f"{header.group(1)}: {header.group(2)}"
        index += 1
        if index < len(lines) and SEPARATOR.match(lines[index]):
            index += 1
        body: list[str] = []
        while index < len(lines) and not SEPARATOR.match(lines[index]) and not HEADER.match(lines[index]):
            body.append(lines[index])
            index += 1
        found.append((title, body))
    return found


def escape(text: str) -> str:
    # 工作流命令的转义规则：% 和换行要编码，不然注解在第一个换行处就断了
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    # title= 这种属性值里 : 和 , 是分隔符，也要编码
    return escape(text).replace(":", "%3A").replace(",", "%2C")


def main(paths: list[str]) -> None:
    # Windows 上 stdout 默认是 cp1252，回溯里的中文一打印就 UnicodeEncodeError，注解一条都出不来
    sys.stdout.reconfigure(encoding="utf-8")
    shown = 0
    for path in paths:
        log_file = Path(path)
        if not log_file.exists():
            continue
        for title, body in failures(log_file.read_text(encoding="utf-8", errors="replace")):
            if shown >= MAX_ANNOTATIONS:
                return
            # 回溯最有用的是最后几行（断言和异常），太长时留尾巴
            tail = body[-MAX_LINES_PER_FAILURE:]
            print(f"::error title={escape_property(title)}::{escape(chr(10).join(tail))}")
            shown += 1


if __name__ == "__main__":
    main(sys.argv[1:])
