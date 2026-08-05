import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # repo root, so `import exec_tracer` works
sys.path.insert(0, str(HERE))  # this folder, so `import toy_module` works

from exec_tracer import trace
import toy_module  # <- replace with your own module

with trace(root=str(HERE)) as run:  # <- point at your project's source folder
    result = toy_module.main()  # <- replace with your own call

run.save(str(HERE / "trace.md"))
