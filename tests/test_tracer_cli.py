import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tracer_cli


def _write_module(root: Path, name: str) -> None:
    """Writes a throwaway module under a name unique to the caller, so
    sys.modules (which caches imports for the process's whole life) can't
    make one test silently reuse another test's already-deleted module."""
    (root / f"{name}.py").write_text("def leaf():\n    return 1\n\ndef main():\n    return leaf() + 1\n")


def _write_notebook(path: Path, cells: list[dict]) -> None:
    path.write_text(json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))


class TracerCliTests(unittest.TestCase):
    def test_py_script_traces_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_module(root, "mymod_py")
            script = root / "run.py"
            script.write_text("import mymod_py\nmymod_py.main()\n")
            out = root / "trace.md"

            with contextlib.redirect_stdout(io.StringIO()):
                rc = tracer_cli.main(["--py", str(script), "--root", str(root), "--save", str(out)])

            self.assertEqual(rc, 0)
            content = out.read_text()
            self.assertIn("mymod_py.main", content)
            self.assertIn("mymod_py.leaf", content)

    def test_kind_both_writes_two_files_and_not_the_original_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_module(root, "mymod_both")
            script = root / "run.py"
            script.write_text("import mymod_both\nmymod_both.main()\n")
            out = root / "trace.md"

            with contextlib.redirect_stdout(io.StringIO()):
                tracer_cli.main(["--py", str(script), "--root", str(root), "--save", str(out), "--kind", "both"])

            self.assertTrue((root / "trace_flowchart.md").exists())
            self.assertTrue((root / "trace_sequence.md").exists())
            self.assertFalse(out.exists())

    def test_notebook_strips_magics_and_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_module(root, "mymod_nb")
            _write_notebook(
                root / "nb.ipynb",
                [
                    {"cell_type": "code", "source": ["%matplotlib inline\n", "import mymod_nb\n"]},
                    {"cell_type": "markdown", "source": ["not code\n"]},
                    {"cell_type": "code", "source": ["mymod_nb.main()\n"]},
                ],
            )
            out = root / "trace.md"

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                rc = tracer_cli.main(["--notebook", str(root / "nb.ipynb"), "--root", str(root), "--save", str(out)])

            self.assertEqual(rc, 0)
            self.assertIn("%matplotlib inline", stderr.getvalue())
            self.assertIn("mymod_nb.main", out.read_text())
            self.assertFalse((root / "nb.py").exists())

    def test_notebook_does_not_overwrite_an_existing_same_named_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "nb.py"
            existing.write_text("SENTINEL = 1\n")
            _write_notebook(root / "nb.ipynb", [{"cell_type": "code", "source": ["1 + 1\n"]}])

            with self.assertRaises(FileExistsError):
                tracer_cli.main(
                    ["--notebook", str(root / "nb.ipynb"), "--root", str(root), "--save", str(root / "trace.md")]
                )

            self.assertEqual(existing.read_text(), "SENTINEL = 1\n")

    def test_py_and_notebook_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tracer_cli.main(["--py", "a.py", "--notebook", "b.ipynb", "--root", ".", "--save", "out.md"])

    def test_target_is_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tracer_cli.main(["--root", ".", "--save", "out.md"])


if __name__ == "__main__":
    unittest.main()
