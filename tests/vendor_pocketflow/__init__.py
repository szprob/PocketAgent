"""上游 PocketFlow 的测试原样放在这个目录（见 pocketflow/VENDORED.md），上游文件不改。

这个 __init__.py 是本项目加的：用 unittest 的 load_tests 钩子，在 Windows 上跳过两条上游用例。
它们假设并行分支按启动顺序跑完（聚合节点按 0..n-1 取结果），而 Python 3.12 及以前 Windows 上
time.monotonic() 精度约 15ms，同时到期的定时器谁先出堆不固定，分支完成顺序跟着乱，就会 KeyError。
这是上游用例自己的时序假设，和 pocketflow 本身、和本项目代码都无关。
"""

import os
import sys
import unittest

WINDOWS_TIMER_ORDER_REASON = "上游用例假设并行分支按启动顺序完成，Windows 计时精度粗，完成顺序不固定"
WINDOWS_TIMER_ORDER_TESTS = {
    "tests.vendor_pocketflow.test_async_parallel_batch_flow.TestAsyncParallelBatchFlow.test_parallel_batch_flow",
    "tests.vendor_pocketflow.test_async_parallel_batch_flow.TestAsyncParallelBatchFlow.test_multiple_batch_sizes",
}


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None):
    package_tests = loader.discover(start_dir=os.path.dirname(__file__), pattern=pattern or "test*.py")
    if sys.platform == "win32":
        for test in _iterate_tests(package_tests):
            if test.id() in WINDOWS_TIMER_ORDER_TESTS:
                _mark_skipped(test, WINDOWS_TIMER_ORDER_REASON)
    standard_tests.addTests(package_tests)
    return standard_tests


def _iterate_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iterate_tests(item)
        else:
            yield item


def _mark_skipped(test: unittest.TestCase, reason: str) -> None:
    """只替换这一个用例实例上的测试方法，同一个类里的其他用例照常跑。"""

    @unittest.skip(reason)
    def skipped() -> None:
        pass

    setattr(test, test._testMethodName, skipped)
