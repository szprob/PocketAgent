"""命令行的 replay / eval 子命令：解析参数，把进度和对比表打到终端，报告路径打在最后。

主入口（console.main）看到第一个参数是 replay / eval 就交给这里；其他情况照旧，第一个参数当成第一句话。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from pocketagent.cli.console import ConsoleRenderer, Style
from pocketagent.config import ConfigError, default_home_dir
from pocketagent.core import agent as agent_module
from pocketagent.core.events import EventHandler
from pocketagent.evaluation.cases import CaseError
from pocketagent.evaluation.evaluate import EvalRequest, evaluate_cases
from pocketagent.evaluation.replay import ReplayError, ReplayRequest, replay_trace
from pocketagent.evaluation.runner import ProviderFactory
from pocketagent.evaluation.trace_reader import TraceError
from pocketagent.evaluation.workspace import WorkspaceError

# Ctrl-C 退出时的退出码（和 shell 的习惯一样：128 + SIGINT）
INTERRUPTED_EXIT_CODE = 130


class UsageError(ValueError):
    """参数指向的文件不对（trace 找不到、提示词文件读不了）。"""


def main(argv: list[str], provider_factory: ProviderFactory | None = None) -> int:
    command, rest = argv[0], argv[1:]
    if command == "replay":
        return run_replay_command(rest, provider_factory)
    return run_eval_command(rest, provider_factory)


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-m",
        "--model",
        action="append",
        default=[],
        metavar="ID",
        help="用哪个模型跑（配置里的 id / 名称 / 模型名），可以给多次逐个对比",
    )
    parser.add_argument(
        "--system-prompt-file",
        metavar="PATH",
        help="用这个文件的内容整个换掉系统提示词；文件里写 {{default_system_prompt}} 的地方放回默认那份",
    )
    parser.add_argument(
        "--keep-workspaces", action="store_true", help="跑完不删临时工作区副本（路径写进报告），方便看改了什么"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="把每次运行的过程（回复、工具调用）打出来")


def _replay_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pocketagent replay",
        description="按顺序重跑一份 trace 里用户说的话（无人值守、在临时副本里），和原来那次并排对比",
    )
    parser.add_argument("trace", help="trace 文件（~/.pocketagent/traces/*.jsonl），也可以直接写任务 id")
    _add_common_options(parser)
    parser.add_argument(
        "-C",
        "--workspace",
        metavar="DIR",
        help="复制这个目录当工作区；不给就从 trace 的起始检查点恢复（找不到检查点时必须给）",
    )
    return parser


def _eval_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pocketagent eval",
        description="拿评测用例（JSON）逐个模型跑一遍，在副本里检查结果；全部通过退出码为 0",
    )
    parser.add_argument("cases", help="用例文件（.json），或者放用例文件的目录")
    _add_common_options(parser)
    return parser


def run_replay_command(argv: list[str], provider_factory: ProviderFactory | None = None) -> int:
    arguments = _replay_parser().parse_args(argv)
    try:
        request = ReplayRequest(
            trace_path=resolve_trace_path(arguments.trace),
            model_keys=arguments.model,
            system_prompt_template=read_prompt_file(arguments.system_prompt_file),
            system_prompt_file=arguments.system_prompt_file,
            workspace_override=Path(arguments.workspace) if arguments.workspace else None,
            keep_workspaces=arguments.keep_workspaces,
        )
        result = asyncio.run(
            replay_trace(
                request,
                provider_factory=provider_factory or agent_module.create_provider,
                event_handler=_event_handler(arguments.verbose),
            )
        )
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return INTERRUPTED_EXIT_CODE
    except (UsageError, ReplayError, TraceError, WorkspaceError, ConfigError) as error:
        print(f"没法回放：{error}", file=sys.stderr)
        return 1
    print()
    print(result.text_table())
    print(f"\n报告：{result.report_dir}（report.md / report.json）")
    return 0


def run_eval_command(argv: list[str], provider_factory: ProviderFactory | None = None) -> int:
    arguments = _eval_parser().parse_args(argv)
    try:
        request = EvalRequest(
            cases_path=Path(arguments.cases),
            model_keys=arguments.model,
            system_prompt_template=read_prompt_file(arguments.system_prompt_file),
            system_prompt_file=arguments.system_prompt_file,
            keep_workspaces=arguments.keep_workspaces,
        )
        result = asyncio.run(
            evaluate_cases(
                request,
                provider_factory=provider_factory or agent_module.create_provider,
                event_handler=_event_handler(arguments.verbose),
            )
        )
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return INTERRUPTED_EXIT_CODE
    except (UsageError, CaseError, WorkspaceError, ConfigError) as error:
        print(f"没法评测：{error}", file=sys.stderr)
        return 1
    print()
    print(result.text_tables())
    print(f"\n报告：{result.report_dir}（report.md / report.json）")
    return 0 if result.all_passed else 1


def resolve_trace_path(text: str) -> Path:
    """先当文件路径；不是文件就当任务 id，去 ~/.pocketagent/traces 下面找。"""
    path = Path(text).expanduser()
    if path.is_file():
        return path
    by_id = default_home_dir() / "traces" / f"{text}.jsonl"
    if "/" not in text and by_id.is_file():
        return by_id
    raise UsageError(f"找不到 trace：{text}")


def read_prompt_file(path_text: str | None) -> str | None:
    if not path_text:
        return None
    try:
        return Path(path_text).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise UsageError(f"读不了提示词文件 {path_text}：{error}") from error


def _event_handler(verbose: bool) -> EventHandler | None:
    if not verbose:
        return None
    return ConsoleRenderer(Style(enabled=sys.stdout.isatty()))
