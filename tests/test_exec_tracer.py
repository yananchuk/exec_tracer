import dataclasses
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exec_tracer import trace

HERE = Path(__file__).resolve().parent
MODULE = __name__  # matches whatever exec_tracer sees as this module's __name__


def q(func_name):
    return f"{MODULE}.{func_name}"


def leaf():
    return 1


def branch():
    return leaf()


def recurse_a():
    return recurse_b()


def recurse_b():
    return recurse_c()


def recurse_c():
    return leaf()


def child_1():
    return 1


def child_2():
    return 2


def child_3():
    return 3


def child_4():
    return 4


def hub():
    return child_1() + child_2() + child_3() + child_4()


def double(n):
    return n * 2


def returns_none():
    return None


def raises_value_error():
    raise ValueError("boom")


def catches_it():
    try:
        raises_value_error()
    except ValueError:
        return "handled"


def worker_reads_a_file(path, box):
    with open(path) as f:
        box["text"] = f.read()


def spawns_a_thread(path):
    box = {}
    t = threading.Thread(target=worker_reads_a_file, args=(path, box))
    t.start()
    t.join()
    return box["text"]


def concurrent_leaf(n):
    return n * n


def spawns_many_threads(count):
    threads = [threading.Thread(target=concurrent_leaf, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TraceScopingTests(unittest.TestCase):
    def test_traces_functions_within_root(self):
        with trace(root=str(HERE)) as run:
            branch()

        self.assertIn(q("branch"), run.node_modules)
        self.assertIn(q("leaf"), run.node_modules)
        self.assertEqual(run.edge_counts[(q("branch"), q("leaf"))], 1)

    def test_exclude_pattern_drops_matching_function(self):
        with trace(root=str(HERE), exclude=["*.leaf"]) as run:
            branch()

        self.assertIn(q("branch"), run.node_modules)
        self.assertNotIn(q("leaf"), run.node_modules)

    def test_max_depth_caps_recursion(self):
        with trace(root=str(HERE), max_depth=2) as run:
            recurse_a()

        self.assertIn(q("recurse_a"), run.node_modules)
        self.assertIn(q("recurse_b"), run.node_modules)
        self.assertNotIn(q("recurse_c"), run.node_modules)

    def test_synthetic_filenames_are_never_in_scope(self):
        # Regression test: dataclass-generated __init__ methods report
        # co_filename == "<string>". Resolving that like a real path used
        # to land inside root whenever root happened to equal cwd (root="."
        # from a project's own directory), pulling exec_tracer's own
        # bookkeeping classes into the trace. See _scope_and_module.
        @dataclasses.dataclass
        class Point:
            x: int
            y: int

        with trace(root=str(Path.cwd())) as run:
            Point(1, 2)

        self.assertTrue(all("Point" not in qualname for qualname in run.node_modules))


class DiagramRenderingTests(unittest.TestCase):
    def test_repeated_calls_are_deduplicated_with_a_count_label(self):
        def caller():
            for _ in range(3):
                leaf()

        with trace(root=str(HERE)) as run:
            caller()

        self.assertIn("x3", run.to_mermaid())

    def test_wide_fanout_gets_invisible_stacking_edges(self):
        with trace(root=str(HERE)) as run:
            hub()

        self.assertIn("~~~", run.to_mermaid(chain_after=3))

        with trace(root=str(HERE)) as run:
            branch()

        self.assertNotIn("~~~", run.to_mermaid(chain_after=3))

    def test_sequence_view_shows_each_calls_own_args_and_return_value(self):
        def caller():
            double(3)
            double(5)

        with trace(root=str(HERE)) as run:
            caller()

        sequence = run.to_mermaid(kind="sequence")
        self.assertIn("double(n=3)", sequence)
        self.assertIn("double(n=5)", sequence)
        self.assertIn("-->>", sequence)  # a return arrow, not just calls
        self.assertIn(": 6", sequence)
        self.assertIn(": 10", sequence)

    def test_sequence_view_nests_child_calls_before_parents_return(self):
        # Regression test: a flat pass over events used to print a call's
        # return immediately after its call, so a parent that calls another
        # traced function would appear to "return" before its own child had
        # even been called. branch() calls leaf(), so branch's return arrow
        # must come after leaf's whole call/return, not right after branch's
        # own call arrow.
        with trace(root=str(HERE)) as run:
            branch()

        sequence = run.to_mermaid(kind="sequence")
        branch_call = sequence.index(f"caller->>{q('branch')}: branch()")
        leaf_call = sequence.index(f"{q('branch')}->>{q('leaf')}: leaf()")
        branch_return = sequence.index(f"{q('branch')}-->>caller: 1")
        self.assertTrue(branch_call < leaf_call < branch_return)

    def test_sequence_view_marks_a_raised_call_with_a_failed_arrow(self):
        with trace(root=str(HERE)) as run:
            catches_it()

        sequence = run.to_mermaid(kind="sequence")
        self.assertIn(f"{q('raises_value_error')}--x{q('catches_it')}: <raised>", sequence)
        self.assertIn(f"{q('catches_it')}-->>caller: 'handled'", sequence)


class ReturnValueAndFileTrackingTests(unittest.TestCase):
    def test_captures_return_value_and_internals(self):
        def compute():
            total = 40 + 2
            return total

        with trace(root=str(HERE)) as run:
            compute()

        qualname = q("compute")
        self.assertEqual(run.node_return[qualname], "42")
        self.assertIn("total=42", run.node_internals[qualname])

    def test_raised_exception_is_distinguished_from_returning_none(self):
        # sys.setprofile reports both a `return None` and an unhandled
        # exception the same way (a 'return' event with arg=None), so
        # without the opcode check in _call_raised these would be
        # indistinguishable in the recorded trace.
        with trace(root=str(HERE)) as run:
            returns_none()
            catches_it()  # calls raises_value_error() internally

        self.assertEqual(run.node_return[q("returns_none")], "None")
        self.assertEqual(run.node_return[q("raises_value_error")], "<raised>")
        self.assertEqual(run.node_return[q("catches_it")], "'handled'")

    def test_file_events_are_deduplicated_by_path_mode_and_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "data.txt"
            data_path.write_text("hello\n")

            def read_twice():
                with open(data_path) as f:
                    f.read()
                with open(data_path) as f:
                    f.read()

            with trace(root=str(HERE)) as run:
                read_twice()

            table = run.to_files_table()
            self.assertEqual(table.count(str(data_path)), 1)
            self.assertIn("read", table)

    def test_sequence_view_includes_file_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "in.txt"
            out_path = Path(tmp) / "out.txt"
            in_path.write_text("data\n")

            def roundtrip():
                with open(in_path) as f:
                    text = f.read()
                with open(out_path, "w") as f:
                    f.write(text)

            with trace(root=str(HERE)) as run:
                roundtrip()

            sequence = run.to_mermaid(kind="sequence")
            self.assertIn("in.txt", sequence)
            self.assertIn("out.txt", sequence)
            self.assertIn("-->>", sequence)  # read: file -->> caller
            self.assertIn("->>", sequence)   # write: caller ->> file


class ThreadingTests(unittest.TestCase):
    def test_calls_and_file_events_in_a_spawned_thread_are_captured(self):
        # Regression test: sys.setprofile only applies to the thread that
        # calls it, so a function that only ever runs inside a spawned
        # thread used to be invisible - and any file it touched still got
        # recorded (builtins.open is a global monkeypatch, not thread-
        # local) but misattributed to whatever the *main* thread happened
        # to have on its stack at the time, since the call stack used to be
        # a single list shared by every thread.
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "data.txt"
            data_path.write_text("hello from a thread\n")

            with trace(root=str(HERE)) as run:
                result = spawns_a_thread(str(data_path))

            self.assertEqual(result, "hello from a thread\n")
            self.assertIn(q("spawns_a_thread"), run.node_modules)
            self.assertIn(q("worker_reads_a_file"), run.node_modules)

            matching = [fe for fe in run.file_events if fe.path == str(data_path)]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].caller, q("worker_reads_a_file"))

    def test_concurrent_threads_dont_lose_or_duplicate_events(self):
        # 50 threads hitting _record_call at once, guarding against the
        # shared seq counter / events list / node bookkeeping getting
        # corrupted without a lock (a lost increment would show up as
        # fewer than 50 unique seq numbers; a torn dict write could crash
        # outright).
        with trace(root=str(HERE)) as run:
            spawns_many_threads(50)

        matching = [ev for ev in run.events if ev.callee == q("concurrent_leaf")]
        self.assertEqual(len(matching), 50)
        self.assertEqual(len({ev.seq for ev in matching}), 50)


if __name__ == "__main__":
    unittest.main()
