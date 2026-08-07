import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # repo root, so `import exec_tracer` works
sys.path.insert(0, str(HERE))  # this folder, so `import worker` works

from exec_tracer import trace
import worker  # <- replace with your own module

TAGS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliett", "kilo", "lima", "mike", "november", "oscar", "papa",
]


def run_loops():
    for i in range(2):
        worker.step_one(i)
    for i in range(4):
        worker.step_two(i)
    for i in range(10):
        worker.step_three(i)
    for i in range(100):
        worker.step_four(TAGS[i % len(TAGS)])


with trace(root=str(HERE)) as run:  # <- point at your project's source folder
    run_loops()

run.save(str(HERE / "trace.md"))
run.save(str(HERE / "trace_sequence.md"), kind="sequence")
