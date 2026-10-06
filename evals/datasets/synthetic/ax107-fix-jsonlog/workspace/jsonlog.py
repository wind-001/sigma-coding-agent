import json
from datetime import datetime
from pathlib import Path


class JsonLogger:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def log(self, level: str, **fields: object) -> None:
        record = {'ts': datetime.now().isoformat(), 'level': level, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._path.open('a', encoding='utf-8') as fh:
            fh.write(line + '\n')
