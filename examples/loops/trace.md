```mermaid
%%{init: {'maxTextSize': 100000, 'flowchart': {'nodeSpacing': 90, 'rankSpacing': 90}}}%%
flowchart TD
    start(("caller"))
    subgraph demo
    n0["__main__.run_loops<br/>./demo.py:17"]
    end
    subgraph worker
    n1["worker.step_one<br/>./worker.py:1"]
    n2["worker.step_two<br/>./worker.py:5"]
    n3["worker.step_three<br/>./worker.py:9"]
    n4["worker.step_four<br/>./worker.py:13"]
    end
    n0 -->|x100| n4
    n0 -->|x10| n3
    n0 -->|x4| n2
    n0 -->|x2| n1
    start --> n0
    n1 ~~~ n2
    n2 ~~~ n3
    n3 ~~~ n4
```

| Function | File | Input args | Return value | Internal variables (at return) |
| --- | --- | --- | --- | --- |
| __main__.run_loops | ./demo.py:17 | - | None | i=99 |
| worker.step_one | ./worker.py:1 | i=0 | 1 | - |
| worker.step_two | ./worker.py:5 | i=0 | 0 | - |
| worker.step_three | ./worker.py:9 | i=0 | 0 | - |
| worker.step_four | ./worker.py:13 | tag='alpha' | 'ALPHA' | - |
