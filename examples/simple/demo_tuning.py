import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # repo root, so `import exec_tracer` works
sys.path.insert(0, str(HERE))  # this folder, so `import toy_module` works

from exec_tracer import trace
import toy_module

with trace(root=str(HERE), exclude=["*.load_data"]) as run:  # <- try your own exclude/max_depth/arg_maxlen here
    result = toy_module.main()

run.save(str(HERE / "trace_tuning.md"))
