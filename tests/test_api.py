import asyncio
import json
from pathlib import Path
import time

import httpx
import pytest

from comfybridge.api import SpeechRequest, _OwnedAudioResponse, create_app
from comfybridge.audio import AudioResult
from comfybridge.config import AppConfig, ComfyConfig, LimitsConfig, ModelConfig, ServerConfig
from comfybridge.service import BridgeError


class FakeService:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.requests = []
        self.cleaned = 0
        self.error = None

    async def synthesize(self, request, request_id, disconnected=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        path = self.tmp_path / ("audio." + request.response_format)
        path.write_bytes(b"RIFFmockaudio")
        async def cleanup():
            self.cleaned += 1
            path.unlink(missing_ok=True)
        return AudioResult(path, "audio/wav" if request.response_format == "wav" else "audio/mpeg", cleanup_callback=cleanup)


def config(**limits):
    return AppConfig(server=ServerConfig(api_key="test-key"), comfyui=ComfyConfig(base_url="http://mock.invalid"),
                     limits=LimitsConfig(**limits), models=[ModelConfig(id="model")])


def client_for(tmp_path, **limits):
    service = FakeService(tmp_path)
    app = create_app(config(**limits), tmp_path, service=service)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge", headers={"authorization": "Bearer test-key"})
    return client, service


@pytest.mark.asyncio
async def test_models_offline_still_authenticated(tmp_path):
    client, service = client_for(tmp_path)
    async with client:
        response = await client.get("/v1/models")
        assert response.status_code == 200
        assert [m["id"] for m in response.json()["data"]] == ["model"]
        response = await client.get("/v1/models", headers={"authorization": "Bearer wrong"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"
        assert not service.requests


@pytest.mark.asyncio
async def test_success_preserves_text_default_mp3_and_cleans(tmp_path):
    client, service = client_for(tmp_path)
    async with client:
        text = "  Markdown **你好**\n😀 "
        response = await client.post("/v1/audio/speech", json={"model": "model", "voice": "voice", "input": text, "speed": 1.0, "instructions": " \n"})
        assert response.status_code == 200
        assert response.content == b"RIFFmockaudio"
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-should-retry"] == "false"
        assert service.requests[0].input == text
        assert service.requests[0].response_format == "mp3"
        assert service.cleaned == 1
        assert not (tmp_path / "audio.mp3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"input": " \t\n"}, {"input": None}, {"input": 42}, {"model": None},
    {"voice": ["voice"]}, {"response_format": None}, {"response_format": "flac"},
    {"speed": True}, {"speed": "1"}, {"speed": 1.1}, {"speed": None},
    {"instructions": "sensitive prompt"}, {"instructions": None},
    {"private unknown field": "sensitive prompt"}, {"input": "\ud800"},
])
async def test_strict_validation_and_no_sensitive_error_echo(tmp_path, patch):
    client, service = client_for(tmp_path)
    payload = {"model": "model", "voice": "voice", "input": "sensitive prompt"}
    payload.update(patch)
    async with client:
        response = await client.post("/v1/audio/speech", content=json.dumps(payload).encode(), headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert "sensitive prompt" not in response.text
        assert "private unknown" not in response.text
        assert not service.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b'{"model":"model","model":"second"}', b'{"x":NaN}', b'{"speed":1e999}', b'null', b'[]', b'"text"', b'{bad', b'\xff'])
async def test_raw_strict_json(tmp_path, body):
    client, service = client_for(tmp_path)
    async with client:
        response = await client.post("/v1/audio/speech", content=body, headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert not service.requests


@pytest.mark.asyncio
async def test_unicode_codepoints_not_bytes(tmp_path):
    client, service = client_for(tmp_path, max_input_chars=2)
    async with client:
        response = await client.post("/v1/audio/speech", json={"model": "model", "voice": "voice", "input": "😀字"})
        assert response.status_code == 200
        response = await client.post("/v1/audio/speech", json={"model": "model", "voice": "voice", "input": "😀字a"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "input_too_long"


@pytest.mark.asyncio
async def test_auth_before_body_and_chunked_body_limit(tmp_path):
    client, service = client_for(tmp_path, max_body_bytes=20)
    async with client:
        response = await client.post("/v1/audio/speech", content=b"x" * 30, headers={"authorization": "Bearer wrong", "content-type": "application/json"})
        assert response.status_code == 401
        response = await client.post("/v1/audio/speech", content=b"x" * 30, headers={"content-type": "application/json"})
        assert response.status_code == 413
        async def chunks():
            yield b"x" * 11
            yield b"x" * 10
        response = await client.post("/v1/audio/speech", content=chunks(), headers={"content-type": "application/json"})
        assert response.status_code == 413
        assert not service.requests


@pytest.mark.asyncio
async def test_duplicate_authorization_and_content_type(tmp_path):
    client, _ = client_for(tmp_path)
    async with client:
        response = await client.post("/v1/audio/speech", content=b"{}", headers=[("authorization", "Bearer test-key"), ("authorization", "Bearer test-key")])
        assert response.status_code == 401
        response = await client.post("/v1/audio/speech", content=b"{}")
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_possible_submission_never_suggests_retry(tmp_path):
    client, service = client_for(tmp_path)
    service.error = BridgeError("deadline_exceeded", 504, submitted=True)
    async with client:
        response = await client.post("/v1/audio/speech", json={"model": "model", "voice": "voice", "input": "private"})
        assert response.status_code == 504
        assert response.headers["x-should-retry"] == "false"


@pytest.mark.asyncio
async def test_audio_cleanup_even_if_transport_send_fails(tmp_path):
    service = FakeService(tmp_path)
    result = await service.synthesize(SpeechRequest(model="model", voice="voice", input="hello"), "id")
    response = _OwnedAudioResponse(result, request_id="id", deadline=time.monotonic() + 2)
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        raise OSError("broken client")
    with pytest.raises(OSError):
        await response({"type": "http", "method": "POST", "headers": []}, receive, send)
    assert service.cleaned == 1
