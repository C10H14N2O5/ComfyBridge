"""Metadata-only rotating diagnostics. Never formats exception details or arbitrary messages."""
from __future__ import annotations

import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from typing import Any

_FIELDS = frozenset({'request_id', 'prompt_id', 'model', 'voice', 'input_length', 'elapsed_seconds',
                     'queue_seconds', 'generation_seconds', 'code', 'state', 'count', 'pid', 'instance_id'})
_TOKEN = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_EVENT = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


class MetadataFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event_name = getattr(record, 'event', record.msg)
        if not isinstance(event_name, str) or not _EVENT.fullmatch(event_name):
            event_name = 'diagnostic_details_omitted'
        payload: dict[str, Any] = {
            'time': self.formatTime(record, '%Y-%m-%dT%H:%M:%S'),
            'level': record.levelname, 'event': event_name,
        }
        fields = getattr(record, 'metadata', {})
        if isinstance(fields, dict):
            for key in sorted(_FIELDS):
                value = fields.get(key)
                if type(value) in (int, float, bool):
                    if type(value) is not float or math.isfinite(value):
                        payload[key] = value
                elif isinstance(value, str) and _TOKEN.fullmatch(value):
                    payload[key] = value
        # In particular, do not call getMessage(), formatException() or formatStack().
        return json.dumps(payload, ensure_ascii=True, allow_nan=False)[:4096]


def configure_logging(data_dir: Path, config: Any) -> logging.Logger:
    log_dir = Path(data_dir) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('comfybridge')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(log_dir / 'comfybridge.log', maxBytes=config.max_bytes,
                                  backupCount=config.backup_count, encoding='utf-8')
    handler.setFormatter(MetadataFormatter())
    logger.addHandler(handler)
    return logger


def event(name: str, **metadata: Any) -> None:
    logging.getLogger('comfybridge').info(name, extra={'event': name, 'metadata': metadata})
