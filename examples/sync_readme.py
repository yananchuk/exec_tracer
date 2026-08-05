"""Re-runs the demos under examples/ and drops their generated .md output
into ../README.md, between marker comments (`<!-- trace:NAME:start -->` /
`<!-- trace:NAME:end -->`). That keeps the README's example output real
and current, instead of someone pasting it in by hand and it going stale.

Run this after touching exec_tracer.py or either demo script:

    python examples/sync_readme.py
"""

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
README = HERE.parent / "README.md"

DEMOS = [
    (HERE / "simple" / "demo.py", "quickstart", HERE / "simple" / "trace.md"),
    (HERE / "simple" / "demo_tuning.py", "tuning", HERE / "simple" / "trace_tuning.md"),
    (HERE / "pipeline" / "demo.py", "pipeline", HERE / "pipeline" / "trace.md"),
    (HERE / "pipeline" / "demo.py", "pipeline_sequence", HERE / "pipeline" / "trace_sequence.md"),
]


def main():
    # demo.py appears twice above (it saves both the flow and sequence
    # views in one run) - run each distinct script only once.
    scripts = dict.fromkeys(script for script, _, _ in DEMOS)
    for script in scripts:
        subprocess.run([sys.executable, str(script)], check=True, cwd=script.parent)

    text = README.read_text()
    for _, name, trace_path in DEMOS:
        start, end = f"<!-- trace:{name}:start -->", f"<!-- trace:{name}:end -->"
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        content = trace_path.read_text().strip()
        text, count = pattern.subn(f"{start}\n{content}\n{end}", text)
        if count != 1:
            raise SystemExit(f"expected exactly one {start} ... {end} marker pair in README.md, found {count}")

    README.write_text(text)
    print(f"Synced {', '.join(name for _, name, _ in DEMOS)} into {README}")


if __name__ == "__main__":
    main()
