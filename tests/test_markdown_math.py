import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDER_SCRIPT = r"""
globalThis.document = { addEventListener() {}, compatMode: "CSS1Compat" };
const { renderMarkdown } = await import("./pocketagent/web/static/js/ui/markdown.js");
const sources = JSON.parse(process.env.POCKETAGENT_TEST_MARKDOWN);
process.stdout.write(JSON.stringify(sources.map((source) => renderMarkdown(source))));
"""


class MarkdownMathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("没装 Node，跳过前端 Markdown 测试")

    def render(self, sources: list[str]) -> list[str]:
        environment = os.environ.copy()
        environment["POCKETAGENT_TEST_MARKDOWN"] = json.dumps(sources)
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", RENDER_SCRIPT],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_inline_and_display_delimiters(self) -> None:
        inline, dollars, multiline, brackets = self.render(
            [
                r"行内 $E=mc^2$ 和 \(\alpha_1+\beta_2\)",
                r"$$\frac{a}{b}$$",
                "$$\n\\sum_{i=1}^n i^2\n$$",
                "\\[\n\\int_0^1 x^2\\,dx\n\\]",
            ]
        )

        self.assertEqual(inline.count('class="katex"'), 2)
        self.assertNotIn("<em>", inline)
        for html in (dollars, multiline, brackets):
            self.assertIn('class="md-math-block"', html)
            self.assertIn('class="katex-display"', html)

    def test_code_currency_and_incomplete_math_stay_literal(self) -> None:
        sources = [
            "`$x$`",
            "```tex\n$x$\n```",
            "价格是 $5 and $10",
            "\\$5",
            "$$\nx^2",
        ]
        for source, html in zip(sources, self.render(sources), strict=True):
            with self.subTest(source=source):
                self.assertNotIn('class="katex"', html)

    def test_invalid_math_falls_back_to_source(self) -> None:
        html = self.render(["$x_{$"])[0]

        self.assertIn("$x_{$", html)
        self.assertNotIn('class="katex"', html)


if __name__ == "__main__":
    unittest.main()
