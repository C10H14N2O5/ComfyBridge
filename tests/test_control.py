from pathlib import Path
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from comfybridge.config import AppConfig, ServerConfig, ComfyConfig
from comfybridge.control import ControlClient, ControlError, create_control_app, new_identity, publish_identity, read_identity


class Service:
    def __init__(self):
        self.draining = False
        self.resolved = []
    def status(self):
        return {'ready': False, 'draining': self.draining}
    def is_drained(self):
        return True
    def begin_drain(self):
        self.draining = True
    async def resolve_uncertain(self, task_id):
        self.resolved.append(task_id)


@pytest.fixture
def private(tmp_path):
    config = AppConfig(server=ServerConfig(api_key='business-secret'), comfyui=ComfyConfig())
    identity = new_identity(tmp_path, 23123, config, str(uuid.uuid4()))
    service = Service()
    stopped = []
    app = create_control_app(service, identity, stopped.append)
    client = TestClient(app, base_url='http://127.0.0.1:23123', client=('127.0.0.1', 54321))
    headers = {'Authorization': 'Bearer ' + identity['token'], 'X-ComfyBridge-Instance': identity['instance_id']}
    return identity, service, stopped, client, headers


def test_business_key_cannot_admin(private):
    _, service, stopped, client, headers = private
    headers['Authorization'] = 'Bearer business-secret'
    assert client.post('/stop', json={'force': True}, headers=headers).status_code == 401
    assert not stopped and not service.draining


@pytest.mark.parametrize('extra', [{'Origin': 'http://evil.test'}, {'Origin': 'null'}, {'Host': 'evil.test'}, {'Host': 'localhost:23123'}, {'X-ComfyBridge-Instance': str(uuid.uuid4())}])
def test_browser_rebinding_and_wrong_identity_rejected(private, extra):
    _, _, stopped, client, headers = private
    assert client.post('/stop', json={'force': True}, headers={**headers, **extra}).status_code in (403, 409)
    assert not stopped


def test_authenticated_status_has_no_token_or_business_key(private):
    identity, _, _, client, headers = private
    response = client.get('/status', headers=headers)
    assert response.status_code == 200
    assert response.json()['instance_id'] == identity['instance_id']
    assert identity['token'] not in response.text
    assert 'business-secret' not in response.text
    assert response.headers['cache-control'] == 'no-store'


def test_drain_force_and_explicit_resolution(private):
    _, service, stopped, client, headers = private
    assert client.post('/drain', headers=headers).status_code == 200
    assert service.draining
    assert client.post('/stop', json={'force': False}, headers=headers).status_code == 200
    assert client.post('/stop', json={'force': True}, headers=headers).status_code == 200
    assert stopped == [False, True]
    task = str(uuid.uuid4())
    assert client.post('/resolve', json={'task_id': task}, headers=headers).status_code == 400
    assert client.post('/resolve', json={'task_id': task, 'acknowledge': True}, headers=headers).status_code == 200
    assert service.resolved == [task]


@pytest.mark.parametrize('body', [{'force': 1}, {'force': 'true'}, {'force': None}, {'force': False, 'extra': 1}])
def test_stop_strict_body(private, body):
    _, _, stopped, client, headers = private
    assert client.post('/stop', json=body, headers=headers).status_code == 400
    assert not stopped


def test_duplicate_admin_json_keys_rejected(private):
    _, _, stopped, client, headers = private
    assert client.post('/stop', content=b'{"force":false,"force":true}', headers=headers).status_code == 400
    assert not stopped


def test_resolution_errors_sanitized(private):
    _, service, _, client, headers = private
    async def rejected(_):
        raise RuntimeError('sensitive upstream details')
    service.resolve_uncertain = rejected
    response = client.post('/resolve', json={'task_id': str(uuid.uuid4()), 'acknowledge': True}, headers=headers)
    assert response.status_code == 409
    assert 'sensitive' not in response.text


def test_admin_body_bounded(private):
    _, _, _, client, headers = private
    assert client.post('/stop', content=b'x' * 4097, headers=headers).status_code == 413


def test_non_loopback_peer_rejected(private):
    identity, service, stopped, _, headers = private
    client = TestClient(create_control_app(service, identity, stopped.append), base_url='http://127.0.0.1:23123', client=('192.168.1.2', 2))
    assert client.get('/status', headers=headers).status_code == 403


def test_client_checks_full_identity_before_mutation(tmp_path, private):
    identity, _, _, _, _ = private
    # Identity belongs to fixture's same tmp_path.
    publish_identity(tmp_path, identity)
    seen = []
    def respond(request):
        seen.append(request.url.path)
        result = {k: v for k, v in identity.items() if k != 'token'}
        result['instance_id'] = str(uuid.uuid4())
        return httpx.Response(200, json=result)
    client = ControlClient(tmp_path, transport=httpx.MockTransport(respond))
    with pytest.raises(ControlError):
        client.command('stop', {'force': True})
    assert seen == ['/status']


def test_state_wrong_version_or_directory_fails_closed(tmp_path, private):
    identity = private[0]
    publish_identity(tmp_path, identity)
    assert read_identity(tmp_path)['token'] == identity['token']
    identity['version'] = 'wrong'
    publish_identity(tmp_path, identity)
    with pytest.raises(ControlError):
        read_identity(tmp_path)
