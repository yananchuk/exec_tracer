"""Command-line wrapper around exec_tracer: run a script or notebook and
save the resulting Mermaid diagram to a markdown file, without needing a
Python REPL or a notebook of your own to call trace() from.

    uv run tracer_cli.py --py path/to/script.py --root src --save trace.md
    uv run tracer_cli.py --notebook path/to/nb.ipynb --root src --save trace.md --kind sequence

A notebook is run by parsing its .ipynb JSON, dropping lines that are
IPython magics or shell-outs (%..., %%..., !...), and concatenating the
remaining code cells into a temporary .py file under --root before tracing
it. Giving the code a real on-disk path is what makes it visible to
exec_tracer's root-scoping in the first place - see the "notebook cells
have no file" note in the main README. Magics are dropped rather than
executed, so a notebook that does real work inside a magic cell (rather
than calling into imported project code) will trace incompletely; the
CLI prints each dropped line to stderr so that's visible, not silent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from exec_tracer import ExecutionTrace, trace

_MAGIC_LINE = re.compile(r"^\s*[%!]")


def _notebook_to_script(notebook_path: Path) -> tuple[str, list[str]]:
    """Concatenates a notebook's code cells into one script, returning
    (script source, the magic/shell-out lines that got dropped)."""
    data = json.loads(notebook_path.read_text())
    kept: list[str] = []
    dropped: list[str] = []
    for cell in data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", [])
        text = "".join(source) if isinstance(source, list) else source
        for line in text.splitlines(keepends=True):
            if _MAGIC_LINE.match(line):
                dropped.append(line.rstrip("\n"))
            else:
                kept.append(line)
        if kept and not kept[-1].endswith("\n"):
            kept.append("\n")
    return "".join(kept), dropped


def _run_script(script_path: Path, root: Path, **trace_kwargs) -> ExecutionTrace:
    """Runs script_path under trace(), adding its directory to sys.path
    first since exec() - unlike `python script.py` - doesn't do that for
    us, and a script that imports a sibling module would otherwise fail."""
    code = compile(script_path.read_text(), str(script_path), "exec")
    globs = {"__name__": "__main__", "__file__": str(script_path)}
    script_dir = str(script_path.parent)
    sys.path.insert(0, script_dir)
    try:
        with trace(root=str(root), **trace_kwargs) as run:
            exec(code, globs)
    finally:
        sys.path.remove(script_dir)
    return run


def _run_notebook(notebook_path: Path, root: Path, **trace_kwargs) -> ExecutionTrace:
    script_source, dropped = _notebook_to_script(notebook_path)
    for line in dropped:
        print(f"tracer_cli: skipping magic/shell line: {line}", file=sys.stderr)
    tmp_path = root / f"{notebook_path.stem}.py"
    if tmp_path.exists():
        raise FileExistsError(
            f"{tmp_path} already exists - tracer_cli writes the notebook's code here "
            "temporarily and won't overwrite an existing file, rename it or pick a "
            "different --root"
        )
    tmp_path.write_text(script_source)
    try:
        return _run_script(tmp_path, root, **trace_kwargs)
    finally:
        tmp_path.unlink(missing_ok=True)


def _save(run: ExecutionTrace, save_path: Path, kind: str, **render_kwargs) -> list[Path]:
    kinds = ["flowchart", "sequence"] if kind == "both" else [kind]
    written = []
    for k in kinds:
        out = save_path if len(kinds) == 1 else save_path.with_stem(f"{save_path.stem}_{k}")
        run.save(str(out), kind=k, **render_kwargs)
        written.append(out)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trace a script or notebook and render it as Mermaid.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--py", type=Path, help="Python script to run and trace")
    target.add_argument("--notebook", type=Path, help="Jupyter notebook to run and trace")
    parser.add_argument("--root", type=Path, required=True, help="source root to scope tracing to")
    parser.add_argument("--save", type=Path, required=True, help="where to write the resulting markdown")
    parser.add_argument("--kind", choices=["flowchart", "sequence", "both"], default="flowchart")
    parser.add_argument("--max-edges", type=int, default=400)
    parser.add_argument("--chain-after", type=int, default=3)
    parser.add_argument("--loop-collapse-after", type=int, default=5)
    parser.add_argument("--exclude", nargs="*", default=None, help="fnmatch patterns of qualnames to exclude")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--arg-maxlen", type=int, default=70)
    parser.add_argument("--no-track-files", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    trace_kwargs = {
        "exclude": args.exclude,
        "max_depth": args.max_depth,
        "arg_maxlen": args.arg_maxlen,
        "track_files": not args.no_track_files,
    }

    if args.notebook:
        run = _run_notebook(args.notebook.resolve(), root, **trace_kwargs)
    else:
        run = _run_script(args.py.resolve(), root, **trace_kwargs)

    render_kwargs = {
        "max_edges": args.max_edges,
        "chain_after": args.chain_after,
        "loop_collapse_after": args.loop_collapse_after,
    }
    for path in _save(run, args.save, args.kind, **render_kwargs):
        print(f"tracer_cli: wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
