"""Trace real code execution with sys.setprofile and render it as Mermaid.

    from exec_tracer import trace

    with trace(root="src") as run:
        do_the_real_thing()

    run.show()                  # render inline in the notebook (no CDN, no server)
    print(run.to_markdown())    # diagram + reference table, e.g. to paste into a .md file
    run.stats()                 # per-function call count / total time

Nothing about the traced code needs to change and there's nothing to
decorate. The profiler attaches to the interpreter for the lifetime of the
`with` block and detaches when it exits. Besides the call graph, it also
grabs each function's return value, a snapshot of its local variables at
return, and which files got read or written (see track_files below if you
want to turn that last part off).
"""

from __future__ import annotations

import builtins
import dis
import fnmatch
import os
import sys
import sysconfig
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _safe_repr(value, maxlen: int = 30) -> str:
    """repr() a value for display without falling into the classic trap of
    dumping a full DataFrame or huge collection into a diagram label."""
    try:
        tname = type(value).__name__
        if tname in ("DataFrame", "Series", "Index"):
            return f"<{tname} shape={getattr(value, 'shape', '?')}>"
        if tname == "ndarray":
            return f"<ndarray shape={value.shape} dtype={value.dtype}>"
        if isinstance(value, (list, tuple, set, dict)) and len(value) > 10:
            return f"<{tname} len={len(value)}>"
        s = repr(value)
    except Exception:
        return f"<{type(value).__name__}>"
    if len(s) > maxlen:
        s = s[: maxlen - 1] + "…"
    return s


def _format_args(frame, maxlen: int = 70) -> str:
    code = frame.f_code
    argcount = code.co_argcount + code.co_kwonlyargcount
    names = code.co_varnames[:argcount]
    parts = []
    for name in names:
        if name in ("self", "cls"):
            continue
        if name in frame.f_locals:
            parts.append(f"{name}={_safe_repr(frame.f_locals[name])}")
    text = ", ".join(parts)
    if len(text) > maxlen:
        text = text[: maxlen - 1] + "…"
    return text


_RETURN_OPCODES = frozenset({"RETURN_VALUE", "RETURN_CONST"})


def _call_raised(frame) -> bool:
    """True if this frame is unwinding because of an exception rather than
    a normal return. sys.setprofile reports both the same way, a 'return'
    event with arg=None, so the only way to tell them apart is to check
    which bytecode instruction the frame was actually at: a real return
    always ends on RETURN_VALUE or RETURN_CONST, an exception doesn't."""
    op = frame.f_code.co_code[frame.f_lasti]
    return dis.opname[op] not in _RETURN_OPCODES


_SKIP_SUFFIXES = {".py", ".pyc", ".pyo", ".so", ".dist-info", ".egg-info", ".egg-link", ".pth"}
_SKIP_PREFIXES = tuple(
    p for p in (sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["purelib"], sys.prefix) if p
)


def _is_tracked_data_file(path: str) -> bool:
    """True unless this looks like the import machinery's own open() call
    (.py/.pyc, site-packages, stdlib), so 'files touched' reports data
    files rather than every module Python happened to load."""
    p = Path(path)
    if p.suffix in _SKIP_SUFFIXES:
        return False
    resolved = str(p)
    return not resolved.startswith(_SKIP_PREFIXES)


def _classify_mode(mode: str) -> str:
    if "+" in mode:
        return "read/write"
    if any(c in mode for c in "wax"):
        return "write"
    return "read"


@dataclass
class _CallEvent:
    seq: int
    caller: Optional[str]
    callee: str
    start: float
    end: Optional[float] = None
    args_str: Optional[str] = None
    retval_str: Optional[str] = None
    raised: bool = False


@dataclass
class _FileEvent:
    seq: int
    caller: Optional[str]
    path: str
    mode: str
    direction: str


class ExecutionTrace:
    """Recorded calls for one `with trace(...)` block, plus rendering helpers."""

    def __init__(self):
        self.events: list[_CallEvent] = []
        self.edge_counts: dict[tuple[Optional[str], str], int] = defaultdict(int)
        self.node_modules: dict[str, str] = {}
        self.node_files: dict[str, str] = {}      # qualname -> "file.py:L<n>"
        self.node_args: dict[str, str] = {}       # qualname -> args from its first call, formatted
        self.node_first_seq: dict[str, int] = {}  # qualname -> seq number of its first call
        self.node_return: dict[str, str] = {}     # qualname -> repr of its first return value
        self.node_internals: dict[str, str] = {}  # qualname -> local vars at that first return
        self.file_events: list[_FileEvent] = []
        # Each thread gets its own call stack: two threads running traced
        # code at once must not see or pop each other's frames. A plain
        # list here would let that happen (and did, before this existed -
        # a file read on thread B would get attributed to whatever thread A
        # happened to have on top of one shared stack).
        self._local = threading.local()
        # Everything below is genuinely shared across threads (one combined
        # trace, not one per thread), so mutating it needs a real lock:
        # CPython doesn't guarantee `self._seq += 1` or dict/list mutation
        # is atomic across bytecode instructions, only within one of them.
        self._lock = threading.Lock()
        # One counter shared by calls and file events, so the two merge into
        # a single chronological timeline in _to_sequence: seq 7 happened
        # after whatever holds seq 6, whether that's a call or a file event.
        self._seq = 0

    @property
    def _stack(self) -> list[_CallEvent]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def _record_call(
        self,
        qualname: str,
        module: str,
        filename: str,
        lineno: int,
        args_str: Optional[str],
    ) -> None:
        # Caller is the top of the *recorded* stack rather than the real one,
        # so a library frame in between (say, df.apply calling back into your
        # code) doesn't break the caller -> callee edge you actually care about.
        stack = self._stack
        caller = stack[-1].callee if stack else None
        with self._lock:
            self._seq += 1
            seq = self._seq
            ev = _CallEvent(seq=seq, caller=caller, callee=qualname, start=time.perf_counter(), args_str=args_str)
            self.events.append(ev)
            self.edge_counts[(caller, qualname)] += 1
            self.node_modules.setdefault(qualname, module)
            self.node_first_seq.setdefault(qualname, seq)
            if qualname not in self.node_files:
                self.node_files[qualname] = f"{Path(filename).name}:{lineno}"
            if args_str is not None and qualname not in self.node_args:
                self.node_args[qualname] = args_str
        stack.append(ev)

    def _record_return(self, frame, retval) -> None:
        stack = self._stack
        if stack:
            qualname = stack[-1].callee
            raised = _call_raised(frame)
            if raised:
                retval_str = "<raised>"
            else:
                retval_str = _safe_repr(retval, maxlen=70) if retval is not None else "None"
            with self._lock:
                if qualname not in self.node_return:
                    self.node_return[qualname] = retval_str
                if qualname not in self.node_internals:
                    code = frame.f_code
                    arg_names = set(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
                    arg_names.update({"self", "cls"})
                    parts = []
                    for name, val in frame.f_locals.items():
                        if name in arg_names or name.startswith("__"):
                            continue
                        parts.append(f"{name}={_safe_repr(val)}")
                        if len(parts) >= 6:  # cap how many vars we show, not just the text length
                            break
                    text = ", ".join(parts)
                    if len(text) > 100:
                        text = text[:99] + "…"
                    self.node_internals[qualname] = text or "-"
            ev = stack.pop()
            ev.end = time.perf_counter()
            ev.retval_str = retval_str
            ev.raised = raised

    def _record_file_event(self, path: str, mode: str, direction: str, caller: Optional[str]) -> None:
        with self._lock:
            self._seq += 1
            self.file_events.append(_FileEvent(seq=self._seq, caller=caller, path=path, mode=mode, direction=direction))

    def stats(self):
        agg: dict[str, dict] = {}
        for ev in self.events:
            if ev.end is None:
                continue
            row = agg.setdefault(ev.callee, {"calls": 0, "total_s": 0.0})
            row["calls"] += 1
            row["total_s"] += ev.end - ev.start
        rows = sorted(
            ({"function": k, **v} for k, v in agg.items()),
            key=lambda r: r["total_s"],
            reverse=True,
        )
        for r in rows:
            r["total_s"] = round(r["total_s"], 6)
        try:
            import pandas as pd

            return pd.DataFrame(rows)
        except ImportError:
            return rows

    def _node_id(self, qualname: str, ids: dict[str, str]) -> str:
        if qualname not in ids:
            ids[qualname] = f"n{len(ids)}"
        return ids[qualname]

    @staticmethod
    def _label(qualname: str) -> str:
        return ".".join(qualname.split(".")[-2:])

    @staticmethod
    def _mermaid_escape(text: str) -> str:
        return text.replace('"', "'")

    @staticmethod
    def _md_escape(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    def to_mermaid(self, kind: str = "flowchart", max_edges: int = 400, chain_after: int = 3) -> str:
        if kind == "flowchart":
            return self._to_flowchart(max_edges, chain_after)
        if kind == "sequence":
            return self._to_sequence(max_edges)
        raise ValueError(f"kind must be 'flowchart' or 'sequence', got {kind!r}")

    def _to_flowchart(self, max_edges: int, chain_after: int) -> str:
        ids: dict[str, str] = {}
        by_module: dict[str, list[str]] = defaultdict(list)
        lines = [
            "%%{init: {'flowchart': {'nodeSpacing': 90, 'rankSpacing': 90}}}%%",
            "flowchart TD",
            '    start(("caller"))',
        ]

        for qualname, module in self.node_modules.items():
            nid = self._node_id(qualname, ids)
            label = self._mermaid_escape(self._label(qualname))
            file_line = self._mermaid_escape(self.node_files.get(qualname, ""))
            by_module[module].append(f'    {nid}["{label}<br/>{file_line}"]')

        for module, node_lines in by_module.items():
            lines.append(f"    subgraph {module}")
            lines.extend(node_lines)
            lines.append("    end")

        edges = sorted(self.edge_counts.items(), key=lambda kv: -kv[1])[:max_edges]

        # When did each (caller, callee) edge first happen? Used below to
        # put sibling-chaining in the order things actually executed.
        first_seen: dict[tuple[Optional[str], str], int] = {}
        for ev in self.events:
            key = (ev.caller, ev.callee)
            if key not in first_seen:
                first_seen[key] = ev.seq

        children_by_parent: dict[Optional[str], list[tuple[int, str]]] = defaultdict(list)

        for (caller, callee), count in edges:
            a = "start" if caller is None else self._node_id(caller, ids)
            b = self._node_id(callee, ids)
            label = f"|x{count}|" if count > 1 else ""
            lines.append(f"    {a} -->{label} {b}")
            children_by_parent[caller].append((first_seen.get((caller, callee), 0), b))

        # Mermaid spreads same-rank nodes sideways, so a parent with a lot
        # of children would otherwise render as one wide row. Chaining them
        # together with invisible edges, in call order, forces a vertical
        # stack instead. That's what actually keeps the diagram narrow.
        for children in children_by_parent.values():
            if len(children) > chain_after:
                children.sort()
                for (_, prev_id), (_, next_id) in zip(children, children[1:]):
                    lines.append(f"    {prev_id} ~~~ {next_id}")

        if self.file_events:
            self._append_files_subgraph(lines, ids)

        return "\n".join(lines)

    def _append_files_subgraph(self, lines: list[str], ids: dict[str, str]) -> None:
        file_node_decls: list[str] = []
        file_edges: list[str] = []
        file_ids: dict[str, str] = {}
        edge_seen: set[tuple[str, str, str]] = set()

        for fe in self.file_events:
            if fe.path not in file_ids:
                fid = f"f{len(file_ids)}"
                file_ids[fe.path] = fid
                short = self._mermaid_escape(Path(fe.path).name or fe.path)
                file_node_decls.append(f'    {fid}[("{short}")]')
            fid = file_ids[fe.path]
            caller_id = "start" if fe.caller is None else ids.get(fe.caller)
            if caller_id is None:
                continue
            if fe.direction == "write":
                key, edge = (caller_id, fid, "w"), f"    {caller_id} -.->|write| {fid}"
            elif fe.direction == "read/write":
                key, edge = (caller_id, fid, "rw"), f"    {caller_id} -.->|r/w| {fid}"
            else:
                key, edge = (caller_id, fid, "r"), f"    {fid} -.->|read| {caller_id}"
            if key not in edge_seen:
                edge_seen.add(key)
                file_edges.append(edge)

        if not file_node_decls:
            return

        lines.append("    subgraph files")
        lines.extend(file_node_decls)
        lines.append("    end")
        lines.extend(file_edges)

    def _to_sequence(self, max_edges: int) -> str:
        # A flat pass over self.events (in call order) would print each
        # call's return right after its call, which is wrong the moment one
        # traced function calls another: the parent's return has to wait
        # until everything it called has finished. So this groups every
        # call and file event by who did it (ev.caller / fe.caller), sorted
        # by the shared seq counter, and walks it as a tree: a function's
        # own children get rendered before its return arrow does.
        events = self.events[:max_edges]

        # Participants are declared upfront in `box` blocks - the sequence
        # diagram's equivalent of a flowchart's subgraph - so functions
        # group visually by module and files get a box of their own,
        # mirroring the module subgraphs and files subgraph in the diagram
        # above. The caller is an actor instead of a participant, the same
        # way it's drawn as a circle instead of a box in the flowchart.
        by_module: dict[str, list[str]] = defaultdict(list)
        seen_funcs: set[str] = set()
        for ev in events:
            for qualname in (ev.caller, ev.callee):
                if qualname and qualname not in seen_funcs:
                    seen_funcs.add(qualname)
                    by_module[self.node_modules.get(qualname, "")].append(qualname)

        file_ids: dict[str, str] = {}
        for fe in self.file_events:
            file_ids.setdefault(fe.path, f"file{len(file_ids)}")

        lines = ["sequenceDiagram", "    actor caller"]
        for module, funcs in by_module.items():
            lines.append(f"    box {module}")
            for qualname in funcs:
                lines.append(f"        participant {self._label(qualname)} as {qualname}")
            lines.append("    end")
        if file_ids:
            # Mermaid sequence diagrams only offer two participant shapes,
            # rectangle (participant) or stick figure (actor) - nothing
            # like the flowchart's cylinder for a "data store" concept. So
            # files stay rectangles too; the `files` box (grouping + label)
            # is what sets them apart here, not a shape.
            lines.append("    box files")
            for path, fid in file_ids.items():
                short = self._mermaid_escape(Path(path).name or path)
                lines.append(f'        participant {fid} as {short}')
            lines.append("    end")

        children: dict[Optional[str], list] = defaultdict(list)
        for ev in events:
            children[ev.caller].append((ev.seq, ev))
        for fe in self.file_events:
            children[fe.caller].append((fe.seq, fe))
        for items in children.values():
            items.sort(key=lambda pair: pair[0])

        # Iterative depth-first walk instead of real recursion: a recursive
        # `render` would use one Python stack frame per level of *traced*
        # call depth, so a deeply recursive traced program (a few hundred
        # levels is enough) blows Python's own recursion limit before the
        # diagram is even rendered. The explicit `stack` here stands in for
        # the call stack a recursive version would use.
        #
        # Each entry is [caller_label, children, index, finish]: children/
        # index track which child of this frame is being visited, and
        # finish (None for the synthetic root) holds what to print once
        # every child has been rendered - the deferred "return arrow" a
        # recursive call would otherwise print right after its call returns.
        stack = [["caller", children.get(None, []), 0, None]]
        while stack:
            caller_label, kids, idx, finish = stack[-1]
            if idx >= len(kids):
                stack.pop()
                if finish is not None:
                    callee_label, retval_str, raised, parent_label = finish
                    # --x is Mermaid's "failed message" arrow: a dotted line
                    # ending in a cross instead of an arrowhead, so a call
                    # that raised looks visibly different from one that
                    # actually returned something.
                    arrow = "--x" if raised else "-->>"
                    lines.append(f"    {callee_label}{arrow}{parent_label}: {self._mermaid_escape(retval_str)}")
                continue
            stack[-1][2] += 1
            _, item = kids[idx]
            if isinstance(item, _CallEvent):
                callee_label = self._label(item.callee)
                func_name = item.callee.split(".")[-1]
                call_desc = self._mermaid_escape(item.args_str) if item.args_str else ""
                lines.append(f"    {caller_label}->>{callee_label}: {func_name}({call_desc})")
                finish = None
                if item.retval_str is not None:
                    finish = (callee_label, item.retval_str, item.raised, caller_label)
                stack.append([callee_label, children.get(item.callee, []), 0, finish])
            else:
                fid = file_ids[item.path]
                if item.direction == "read":
                    lines.append(f"    {fid}-->>{caller_label}: read")
                else:
                    lines.append(f"    {caller_label}->>{fid}: {item.direction}")
        return "\n".join(lines)

    def to_reference_table(self) -> str:
        """One row per recorded function: full qualified name, file:line,
        input args, return value, and a few local variables at return."""
        rows = sorted(self.node_modules.keys(), key=lambda q: self.node_first_seq.get(q, 0))
        lines = [
            "| Function | File | Input args | Return value | Internal variables (at return) |",
            "| --- | --- | --- | --- | --- |",
        ]
        for q in rows:
            func = self._md_escape(q)
            file_ = self._md_escape(self.node_files.get(q, ""))
            args = self._md_escape(self.node_args.get(q, "")) or "-"
            ret = self._md_escape(self.node_return.get(q, "")) or "-"
            internals = self._md_escape(self.node_internals.get(q, "")) or "-"
            lines.append(f"| {func} | {file_} | {args} | {ret} | {internals} |")
        return "\n".join(lines)

    def to_files_table(self) -> str:
        if not self.file_events:
            return ""
        lines = ["**Files touched:**", "", "| File | Mode | Direction | Called from |", "| --- | --- | --- | --- |"]
        seen: set[tuple[str, str, Optional[str]]] = set()
        for fe in self.file_events:
            key = (fe.path, fe.mode, fe.caller)
            if key in seen:
                continue
            seen.add(key)
            path_disp = self._md_escape(fe.path)
            caller_disp = self._md_escape(self._label(fe.caller) if fe.caller else "caller")
            lines.append(f"| {path_disp} | `{fe.mode}` | {fe.direction} | {caller_disp} |")
        return "\n".join(lines)

    def to_markdown(self, kind: str = "flowchart", max_edges: int = 400, chain_after: int = 3) -> str:
        diagram = self.to_mermaid(kind=kind, max_edges=max_edges, chain_after=chain_after)
        parts = [f"```mermaid\n{diagram}\n```"]
        if kind == "flowchart":
            parts.append(self.to_reference_table())
            files_table = self.to_files_table()
            if files_table:
                parts.append(files_table)
        return "\n\n".join(parts)

    def save(self, path, kind: str = "flowchart", max_edges: int = 400, chain_after: int = 3) -> None:
        Path(path).write_text(self.to_markdown(kind=kind, max_edges=max_edges, chain_after=chain_after) + "\n")

    def show(self, kind: str = "flowchart", max_edges: int = 400, chain_after: int = 3):
        from IPython.display import Markdown, display

        display(Markdown(self.to_markdown(kind=kind, max_edges=max_edges, chain_after=chain_after)))


class trace:
    """Context manager that traces execution via sys.setprofile, scoped to
    `root`. Pass track_files=False to skip wrapping builtins.open if you
    don't need file tracking and want to avoid the (small) overhead."""

    def __init__(
        self,
        root,
        exclude: Optional[list[str]] = None,
        max_depth: Optional[int] = None,
        arg_maxlen: int = 70,
        track_files: bool = True,
    ):
        self.root = Path(root).resolve()
        self.exclude = exclude or []
        self.max_depth = max_depth
        self.arg_maxlen = arg_maxlen
        self.track_files = track_files
        self.result = ExecutionTrace()
        self._local = threading.local()  # _skip_stack is per-thread, same reason ExecutionTrace._stack is
        self._scope_cache: dict[str, tuple[bool, str]] = {}
        self._prev_profiler = None
        self._real_open = None

    @property
    def _skip_stack(self) -> list[bool]:
        # Mirrors the *real* call stack 1:1, per thread.
        stack = getattr(self._local, "skip_stack", None)
        if stack is None:
            stack = self._local.skip_stack = []
        return stack

    def _scope_and_module(self, filename: str) -> tuple[bool, str]:
        cached = self._scope_cache.get(filename)
        if cached is not None:
            return cached
        # Some filenames aren't real on-disk paths: dataclass-generated
        # __init__ methods, frozen stdlib modules, exec()'d or notebook
        # code all report something like "<string>" as co_filename. Treat
        # those as relative and Path.resolve() just tacks them onto cwd. If
        # cwd happens to equal root, which is exactly what root="." from a
        # project's own directory gives you, that makes them resolve inside
        # root, and they'd get traced as if they were your own code.
        if filename.startswith("<") and filename.endswith(">"):
            in_scope, module = False, ""
        else:
            try:
                rel = Path(filename).resolve().relative_to(self.root)
                in_scope, module = True, (rel.parts[0] if len(rel.parts) > 1 else rel.stem)
            except (ValueError, OSError):
                in_scope, module = False, ""
        self._scope_cache[filename] = (in_scope, module)
        return in_scope, module

    @staticmethod
    def _qualname(frame) -> str:
        code = frame.f_code
        module = frame.f_globals.get("__name__", "")
        cls = ""
        if code.co_varnames:
            first = code.co_varnames[0]
            val = frame.f_locals.get(first)
            if first == "self" and val is not None:
                cls = f"{type(val).__name__}."
            elif first == "cls" and isinstance(val, type):
                cls = f"{val.__name__}."
        return f"{module}.{cls}{code.co_name}"

    def _profiler(self, frame, event, arg):
        if event == "call":
            filename = frame.f_code.co_filename
            in_scope, module = self._scope_and_module(filename)
            record = in_scope
            qualname = None
            if record:
                qualname = self._qualname(frame)
                if any(fnmatch.fnmatch(qualname, pat) for pat in self.exclude):
                    record = False
                elif self.max_depth is not None and len(self.result._stack) >= self.max_depth:
                    record = False
            self._skip_stack.append(record)
            if record:
                # Formatted every call, not just the first, so the sequence
                # view can show each call's actual arguments and return
                # value rather than repeating whatever the first call had.
                args_str = _format_args(frame, maxlen=self.arg_maxlen)
                self.result._record_call(
                    qualname,
                    module,
                    filename=filename,
                    lineno=frame.f_code.co_firstlineno,
                    args_str=args_str,
                )
        elif event == "return":
            # setprofile never sends unmatched 'return's, so this stays balanced
            if self._skip_stack and self._skip_stack.pop():
                self.result._record_return(frame, arg)

    def _wrap_open(self):
        real_open = self._real_open
        result = self.result

        def wrapped_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            try:
                if isinstance(file, (str, os.PathLike)):
                    path_str = os.fspath(file)
                    if _is_tracked_data_file(path_str):
                        caller = result._stack[-1].callee if result._stack else None
                        result._record_file_event(path_str, mode, _classify_mode(mode), caller)
            except Exception:
                pass  # file tracking must never break the traced code
            return f

        return wrapped_open

    def __enter__(self) -> ExecutionTrace:
        self._prev_profiler = sys.getprofile()
        sys.setprofile(self._profiler)
        # threading.setprofile installs the same profiler on any thread
        # started via threading.Thread from here on, so calls made inside
        # a spawned thread get recorded too, not just the calling thread's
        # own. It doesn't reach back to threads already running before this
        # point, and doesn't cover threads started without the threading
        # module (raw _thread.start_new_thread) - both are limitations of
        # threading.setprofile itself, not something this class works around.
        threading.setprofile(self._profiler)
        if self.track_files:
            self._real_open = builtins.open
            builtins.open = self._wrap_open()
        return self.result

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.track_files and self._real_open is not None:
            builtins.open = self._real_open
            self._real_open = None
        # threading has no getprofile() to restore a prior hook the way
        # sys.getprofile() does for the main thread, so this just clears it.
        # A thread already running when the with block exits keeps whatever
        # profiler it started with until it finishes on its own.
        threading.setprofile(None)
        sys.setprofile(self._prev_profiler)
        return False