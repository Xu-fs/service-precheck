"""检查入口必须传播失败，并停止执行后续步骤。"""

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


class VerifyTests(unittest.TestCase):
    def load_runner(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "verify.py"
        self.assertTrue(path.is_file(), "缺少统一检查入口 scripts/verify.py")
        spec = importlib.util.spec_from_file_location("project_verify", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_failed_step_preserves_exit_code_and_stops_later_steps(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "should-not-exist"
            result = runner.run_steps(
                [
                    ("failure", [sys.executable, "-c", "raise SystemExit(7)"]),
                    (
                        "later",
                        [
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ],
                    ),
                ]
            )
            self.assertEqual(result, 7)
            self.assertFalse(marker.exists())

    def test_missing_command_is_failure(self):
        runner = self.load_runner()
        with tempfile.TemporaryDirectory() as folder:
            self.assertNotEqual(
                runner.run_steps(
                    [
                        ("missing", [str(Path(folder) / "missing-executable")]),
                    ]
                ),
                0,
            )

    def test_successful_step_runs_from_repository_with_source_imports(self):
        runner = self.load_runner()
        result = runner.run_steps(
            [
                (
                    "environment",
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import zhengwu_precheck; "
                        "assert Path('AGENTS.md').is_file()",
                    ],
                )
            ]
        )
        self.assertEqual(result, 0)
