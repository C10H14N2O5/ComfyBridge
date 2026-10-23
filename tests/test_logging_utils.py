import json
import logging
from types import SimpleNamespace

from comfybridge.logging_utils import MetadataFormatter, configure_logging, event


def test_unsafe_messages_and_exceptions_are_not_formatted():
    record = logging.LogRecord('test', logging.ERROR, '', 0, 'PRIVATE original text %s', ('SECRET',), None)
    record.exc_text = 'PRIVATE traceback'
    record.metadata = {'api_key': 'SECRET', 'request_id': 'abc-123', 'input_length': 42,
                       'code': 'backend_failed', 'state': 'PRIVATE text', 'unknown': 'SECRET'}
    value = MetadataFormatter().format(record)
    assert 'PRIVATE' not in value and 'SECRET' not in value
    parsed = json.loads(value)
    assert parsed['request_id'] == 'abc-123'
    assert parsed['input_length'] == 42
    assert 'state' not in parsed


def test_rotation_and_event_whitelist(tmp_path):
    logger = configure_logging(tmp_path, SimpleNamespace(max_bytes=1024, backup_count=2))
    for n in range(60):
        event('request_finished', request_id=f'req-{n}', code='ok', input='PRIVATE ORIGINAL')
    for handler in logger.handlers:
        handler.flush()
    files = list((tmp_path / 'logs').glob('comfybridge.log*'))
    assert len(files) == 3
    assert all(f.stat().st_size <= 1100 for f in files)
    for file in files:
        content = file.read_text()
        assert 'PRIVATE' not in content
        assert all(json.loads(line)['event'] == 'request_finished' for line in content.splitlines())
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
