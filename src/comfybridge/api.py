"""Strict, authenticated OpenAI-compatible speech surface; no management routes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
from pathlib import Path
import re
import time
from typing import Literal
import uuid

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError, field_validator
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from .config import AppConfig, parse_json_strict
from .service import BridgeError, BridgeService


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str
    voice: str
    input: str
    response_format: Literal["wav", "mp3"] = "mp3"
    speed: int | float = 1
    instructions: str = ""
    _deadline: float | None = PrivateAttr(default=None)

    @field_validator("model", "voice")
    @classmethod
    def identifier(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
            raise ValueError("invalid_identifier")
        return value

    @field_validator("input", "instructions")
    @classmethod
    def scalar_text(cls, value):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("invalid_unicode")
        return value

    @field_validator("input")
    @classmethod
    def nonempty_input(cls, value):
        if not value.strip():
            raise ValueError("empty_input")
        return value

    @field_validator("speed", mode="before")
    @classmethod
    def neutral_speed(cls, value):
        if type(value) not in {int, float} or value != 1:
            raise ValueError("unsupported_speed")
        return value

    @field_validator("instructions")
    @classmethod
    def neutral_instructions(cls, value):
        if value.strip():
            raise ValueError("unsupported_instructions")
        return value


_MESSAGES = {
    400: "Invalid or unsupported request.", 401: "Invalid API key.",
    413: "Request body is too large.", 429: "Request capacity is full.",
    499: "Client disconnected.", 502: "Speech processing failed.",
    503: "Speech service is not ready.", 504: "Request deadline exceeded.",
}


def _error(error: BridgeError, request_id: str):
    kind = "invalid_request_error" if error.status in {400, 401, 413} else "server_error"
    headers = {"cache-control": "no-store", "x-request-id": request_id}
    if error.submitted:
        headers["x-should-retry"] = "false"
    if error.status == 401:
        headers["www-authenticate"] = "Bearer"
    return JSONResponse(status_code=error.status, content={"error": {
        "message": _MESSAGES.get(error.status, "Request failed."), "type": kind,
        "param": error.param, "code": error.code,
    }}, headers=headers)


class _OwnedAudioResponse(FileResponse):
    def __init__(self, result, *, request_id: str, deadline: float):
        super().__init__(result.path, media_type=result.media_type, headers={
            "cache-control": "no-store", "x-request-id": request_id,
            "x-should-retry": "false",
        })
        self.result = result
        self.deadline = deadline

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(max(0.001, self.deadline - time.monotonic())):
                await super().__call__(scope, receive, send)
                self.result._delivery_completed = True
        finally:
            # BackgroundTask alone is insufficient when a send raises/cancels.
            with anyio.CancelScope(shield=True):
                await self.result.cleanup()


def create_app(config: AppConfig, data_dir: Path, service: BridgeService | None = None) -> FastAPI:
    config = config.model_copy(deep=True)
    external = service is not None
    bridge = service if service is not None else BridgeService(config, data_dir)

    @asynccontextmanager
    async def lifespan(app):
        if not external:
            await bridge.start()
        try:
            yield
        finally:
            if not external:
                await bridge.close(force=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bridge = bridge

    def authenticate(request: Request):
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            raise BridgeError("invalid_api_key", 401)
        # RFC 7235: the auth-scheme token is case-insensitive; the credential is not.
        scheme, separator, token = values[0].partition(" ")
        expected = config.server.api_key.encode("utf-8")
        if (not separator or scheme.casefold() != "bearer"
                or not hmac.compare_digest(token.encode("utf-8"), expected)):
            raise BridgeError("invalid_api_key", 401)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return _error(BridgeError("invalid_route", error.status_code), str(uuid.uuid4()))

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return _error(BridgeError("internal_error", 502, submitted=True), str(uuid.uuid4()))

    @app.get("/v1/models")
    async def models(request: Request):
        request_id = str(uuid.uuid4())
        try:
            authenticate(request)
        except BridgeError as error:
            return _error(error, request_id)
        return JSONResponse({"object": "list", "data": [
            {"id": model.id, "object": "model", "created": 0, "owned_by": "comfybridge"}
            for model in config.models
        ]}, headers={"cache-control": "no-store", "x-request-id": request_id})

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        request_id = str(uuid.uuid4())
        deadline = time.monotonic() + config.limits.request_timeout
        try:
            authenticate(request)  # Always before consuming or parsing the request body.
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise BridgeError("invalid_content_type", 400)
            length = request.headers.getlist("content-length")
            if length:
                if len(length) != 1 or not re.fullmatch(r"[0-9]+", length[0]) or len(length[0]) > 20:
                    raise BridgeError("invalid_content_length", 400)
                if int(length[0]) > config.limits.max_body_bytes:
                    raise BridgeError("body_too_large", 413)
            body = bytearray()
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > config.limits.max_body_bytes:
                        raise BridgeError("body_too_large", 413)
                    body.extend(chunk)
            try:
                document = parse_json_strict(bytes(body))
                payload = SpeechRequest.model_validate(document)
            except ValidationError as error:
                # Do not serialize Pydantic errors: their input/loc can contain arbitrary text.
                location = error.errors(include_url=False, include_input=False)[0].get("loc", ())
                param = location[0] if location and location[0] in SpeechRequest.model_fields else None
                raise BridgeError("invalid_request", 400, param=param) from None
            except (ValueError, UnicodeError, RecursionError):
                raise BridgeError("invalid_json", 400) from None
            if len(payload.input) > config.limits.max_input_chars:
                raise BridgeError("input_too_long", 400, param="input")
            payload._deadline = deadline
            result = await bridge.synthesize(payload, request_id, disconnected=request.is_disconnected)
            return _OwnedAudioResponse(result, request_id=request_id, deadline=deadline)
        except BridgeError as error:
            return _error(error, request_id)
        except asyncio.TimeoutError:
            return _error(BridgeError("deadline_exceeded", 504), request_id)
        except ClientDisconnect:
            return _error(BridgeError("client_disconnected", 499), request_id)
        except Exception:
            return _error(BridgeError("internal_error", 502, submitted=True), request_id)

    return app
