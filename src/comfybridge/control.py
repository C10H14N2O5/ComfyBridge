"""Private loopback management protocol, intentionally separate from business API."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import uuid
from typing import Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .platform_windows import atomic_private_json, directory_id, private_runtime

from . import __version__

PROTOCOL = 1
PRODUCT = 'ComfyBridge'


class ControlError(RuntimeError):
    pass


def config_digest(config) -> str:
    raw = json.dumps(config.model_dump(mode='json'), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def new_identity(data_dir: Path, port: int, config, snapshot: str) -> dict:
    return {'product': PRODUCT, 'protocol': PROTOCOL, 'version': __version__, 'instance_id': str(uuid.uuid4()),
            'directory_id': directory_id(data_dir), 'pid': os.getpid(), 'port': port,
            'token': secrets.token_urlsafe(48), 'config_digest': config_digest(config), 'snapshot': snapshot}


def publish_identity(data_dir: Path, identity: dict) -> None:
    atomic_private_json(private_runtime(data_dir) / 'control.json', identity)


def read_identity(data_dir: Path) -> dict:
    try:
        path = private_runtime(data_dir) / 'control.json'
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError()
        identity = json.loads(path.read_text(encoding='utf-8'))
        if (identity.get('product') != PRODUCT or identity.get('protocol') != PROTOCOL or identity.get('version') != __version__
                or identity.get('directory_id') != directory_id(data_dir)
                or type(identity.get('port')) is not int or not 0 < identity['port'] < 65536
                or not isinstance(identity.get('token'), str) or len(identity['token']) < 32
                or str(uuid.UUID(identity['instance_id'])) != identity['instance_id']):
            raise ValueError()
        return identity
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ControlError('control_state_unavailable') from None


def remove_identity(data_dir: Path, instance_id: str) -> None:
    try:
        if read_identity(data_dir)['instance_id'] == instance_id:
            (data_dir / 'runtime/control.json').unlink(missing_ok=True)
    except ControlError:
        pass


def create_control_app(service, identity: dict, shutdown: Callable[[bool], None], *, status_extra: Callable[[], dict] | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def security(request: Request, call_next):
        # Reject browser-origin calls outright (including Origin:null), not merely CORS.
        allowed_host = f"127.0.0.1:{identity['port']}"
        if (request.headers.get('host') != allowed_host or 'origin' in request.headers
                or request.client is None or request.client.host != '127.0.0.1'):
            return JSONResponse({'error': 'local_only'}, status_code=403)
        supplied = request.headers.get('authorization', '')
        if not hmac.compare_digest(supplied.encode(), ('Bearer ' + identity['token']).encode()):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        if request.headers.get('x-comfybridge-instance') != identity['instance_id']:
            return JSONResponse({'error': 'identity_mismatch'}, status_code=409)
        if request.method == 'POST':
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4096:
                    return JSONResponse({'error': 'too_large'}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/status')
    async def status():
        result = dict(service.status())
        result.update({key: identity[key] for key in ('product', 'protocol', 'version', 'instance_id', 'directory_id', 'pid', 'config_digest', 'snapshot')})
        result['drained'] = service.is_drained()
        if status_extra:
            result.update(status_extra())
        return result

    @app.post('/drain')
    async def drain():
        service.begin_drain()
        return {'state': 'draining'}

    @app.post('/stop')
    async def stop(request: Request):
        try:
            from .config import parse_json_strict
            body = parse_json_strict(await request.body())
            if body not in ({'force': True}, {'force': False}) or type(body['force']) is not bool:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        service.begin_drain()
        shutdown(body['force'])
        return {'state': 'force_stopping' if body['force'] else 'draining'}

    @app.post('/resolve')
    async def resolve(request: Request):
        try:
            from .config import parse_json_strict
            body = parse_json_strict(await request.body())
            if set(body) != {'task_id', 'acknowledge'} or body['acknowledge'] is not True:
                raise ValueError()
            task_id = str(uuid.UUID(body['task_id']))
            await service.resolve_uncertain(task_id)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({'error': 'invalid_resolution'}, status_code=400)
        except Exception:
            # Backend may reject an in-progress/not-uncertain task. Never echo details.
            return JSONResponse({'error': 'resolution_rejected'}, status_code=409)
        return {'state': 'acknowledged'}

    return app


class ControlClient:
    def __init__(self, data_dir: Path, *, transport=None):
        self.data_dir = data_dir
        self.identity = read_identity(data_dir)
        self.transport = transport

    def request(self, method: str, route: str, body: dict | None = None) -> dict:
        if route not in ('status', 'drain', 'stop', 'resolve'):
            raise ControlError('invalid_control_route')
        identity = self.identity
        headers = {'Authorization': 'Bearer ' + identity['token'], 'X-ComfyBridge-Instance': identity['instance_id']}
        try:
            with httpx.Client(timeout=3, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                response = client.request(method, f"http://127.0.0.1:{identity['port']}/{route}", headers=headers, json=body)
                response.raise_for_status()
                result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            if route == 'status':
                for key in ('product', 'protocol', 'version', 'instance_id', 'directory_id', 'pid', 'config_digest', 'snapshot'):
                    if result.get(key) != identity.get(key):
                        raise ControlError('control_identity_mismatch')
            return result
        except (httpx.HTTPError, ValueError):
            raise ControlError('control_unavailable_or_rejected') from None

    def status(self) -> dict:
        return self.request('GET', 'status')

    def command(self, route: str, body: dict | None = None) -> dict:
        self.status()  # verify nonce identity BEFORE any mutation, never trust PID alone
        return self.request('POST', route, body)


def get_status(data_dir: Path) -> dict | None:
    try:
        return ControlClient(data_dir).status()
    except ControlError:
        return None
