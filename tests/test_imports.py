"""Fresh-interpreter ownership of repository command helpers."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ImportBoundaryTests(unittest.TestCase):
    def test_local_attention_wins_over_unrelated_regular_tools_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            foreign_root = Path(temporary) / "foreign"
            foreign_tools = foreign_root / "tools"
            foreign_tools.mkdir(parents=True)
            (foreign_tools / "__init__.py").write_text('"""Unrelated synthetic tools package."""\n', encoding="utf-8")
            environment = dict(os.environ)
            paths = [str(foreign_root)]
            if environment.get("PYTHONPATH"):
                paths.append(environment["PYTHONPATH"])
            environment["PYTHONPATH"] = os.pathsep.join(paths)
            script = """
from pathlib import Path
import tools
from tools import attention
import collect, consumer, economics, observatory, portfolio, stability
expected = Path.cwd() / "tools"
assert Path(tools.__file__).resolve() == expected / "__init__.py"
assert Path(attention.__file__).resolve() == expected / "attention.py"
assert collect.attention_ledger is attention
assert consumer.attention_ledger is attention
assert economics.attention is attention
assert observatory.attention_ledger is attention
assert stability.attention_ledger is attention
print("repository_attention_selected")
"""
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=PROJECT_ROOT, env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "repository_attention_selected")


if __name__ == "__main__":
    unittest.main()
