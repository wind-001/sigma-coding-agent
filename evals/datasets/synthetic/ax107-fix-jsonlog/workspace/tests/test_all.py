import json
import uuid
from datetime import datetime, timezone

from jsonlog import JsonLogger


def test_datetime_and_uuid(tmp_path) -> None:
    path = tmp_path / 'a.log'
    logger = JsonLogger(path)
    ts = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
    request_id = uuid.UUID('12345678-1234-5678-1234-567812345678')
    logger.log('INFO', user='u1', at=ts, request_id=request_id)
    record = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
    assert record['level'] == 'INFO'
    assert record['at'] == '2026-10-04T12:00:00+00:00'
    assert record['request_id'] == '12345678-1234-5678-1234-567812345678'


def test_native_types_untouched(tmp_path) -> None:
    path = tmp_path / 'b.log'
    logger = JsonLogger(path)
    logger.log('WARN', retries=3, tags=['x'], meta={'k': 1})
    record = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
    assert record['retries'] == 3 and record['tags'] == ['x'] and record['meta'] == {'k': 1}


def test_append_semantics(tmp_path) -> None:
    path = tmp_path / 'c.log'
    logger = JsonLogger(path)
    logger.log('INFO', n=1)
    logger.log('INFO', n=2)
    lines = path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
