import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # repo root, so `import exec_tracer` works
sys.path.insert(0, str(HERE))  # this folder, so `import reader`/`import processor` work
os.chdir(HERE)  # so relative paths below (and in the trace's files-touched table) stay short

from exec_tracer import trace
import processor  # <- replace with your own module; it imports reader.py itself

with trace(root=str(HERE)) as run:  # <- point at your project's source folder
    summary = processor.run_pipeline("data/threshold.txt", "data/readings.txt", "data/output.txt")

run.save("trace.md")                        # deduplicated call graph
run.save("trace_sequence.md", kind="sequence")  # literal chronological call order
