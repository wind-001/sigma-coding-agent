# report.json 格式约定

1. `total`: events.jsonl 的总行数(整数)。
2. `by_level`: 对象,四个键 DEBUG / INFO / WARNING / ERROR **必须全部存在**,值为该级别的事件条数(没有就是 0)。
3. `errors_first`: 所有 level=="ERROR" 事件的 message 列表,保持**在文件中出现的先后顺序**。

用法:python build_report.py —— 读取同目录 events.jsonl,在同目录写 report.json。
