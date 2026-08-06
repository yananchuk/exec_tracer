```mermaid
%%{init: {'maxTextSize': 100000, 'flowchart': {'nodeSpacing': 90, 'rankSpacing': 90}}}%%
flowchart TD
    start(("caller"))
    subgraph toy_module
    n0["toy_module.main<br/>./toy_module.py:13"]
    n1["toy_module.load_data<br/>./toy_module.py:1"]
    n2["toy_module.process<br/>./toy_module.py:5"]
    n3["toy_module.summarize<br/>./toy_module.py:9"]
    end
    start --> n0
    n0 --> n1
    n0 --> n2
    n0 --> n3
```

| Function | File | Input args | Return value | Internal variables (at return) |
| --- | --- | --- | --- | --- |
| toy_module.main | ./toy_module.py:13 | - | 12 | data={'a': 1, 'b': 2, 'c': 3}, processed={'a': 2, 'b': 4, 'c': 6} |
| toy_module.load_data | ./toy_module.py:1 | - | {'a': 1, 'b': 2, 'c': 3} | - |
| toy_module.process | ./toy_module.py:5 | data={'a': 1, 'b': 2, 'c': 3} | {'a': 2, 'b': 4, 'c': 6} | - |
| toy_module.summarize | ./toy_module.py:9 | data={'a': 2, 'b': 4, 'c': 6} | 12 | - |
