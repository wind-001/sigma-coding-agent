"""JSON 持久化：一列任务字典 <-> 一个 JSON 文件。"""

import json
from pathlib import Path


class Storage:
    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, records: list[dict]) -> None:
        self._path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), "utf-8"
        )

    def load(self) -> list[dict]:
        """读出任务字典列表；文件不存在视为空存储。"""
        try:
            text = self._path.read_text("utf-8")
        except FileNotFoundError:
            return []
        return json.loads(text)
