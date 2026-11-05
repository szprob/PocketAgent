"""评测示例的检查：add 修好以后这里才会通过（不在仓库的测试套件里跑）。"""

import unittest

from calc import add, multiply


class CalcTest(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)

    def test_multiply(self) -> None:
        self.assertEqual(multiply(4, 5), 20)


if __name__ == "__main__":
    unittest.main()
