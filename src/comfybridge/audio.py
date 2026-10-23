"""Bounded local media conversion. Never invokes a shell or logs media metadata."""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class AudioError(Exception):
    def __init__(self, code: str, message: str = 'Audio processing failed'):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class AudioResult:
    path: Path
    media_type: str
    cleanup_callback: Callable[..., Any] | None = None
    _owned_paths: tuple[Path, ...] = field(default=(), repr=False)
    _owned_dir: Path | None = field(default=None, repr=False)
    _cleaned: bool = field(default=False, init=False, repr=False)

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            # Do not recurse: only files registered by the converter are owned.
            for path in self._owned_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # startup janitor handles residual owned request directories
            if self._owned_dir is not None:
                try:
                    self._owned_dir.rmdir()
                except OSError:
                    pass
        finally:
            if self.cleanup_callback is not None:
                result = self.cleanup_callback()
                if inspect.isawaitable(result):
                    await result


def resolve_tool(name: str, explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        candidate = Path(explicit)
        if candidate.is_file():
            return candidate.resolve()
        located = shutil.which(str(explicit))
        if located:
            return Path(located).resolve()
        raise AudioError('audio_tool_missing', f'{name} executable is unavailable')
    executable = name + ('.exe' if os.name == 'nt' else '')
    roots = [Path(sys.executable).resolve().parent]
    if getattr(sys, 'frozen', False):
        roots.append(Path(getattr(sys, '_MEIPASS', roots[0])))
    else:
        roots.append(Path(__file__).resolve().parents[2])
    for root in roots:
        candidate = root / 'bin' / executable
        if candidate.is_file():
            return candidate
    # Source development may use a system install; packaged app must be self-contained.
    if not getattr(sys, 'frozen', False):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    raise AudioError('audio_tool_missing', f'{name} executable is unavailable')


class AudioProcessor:
    def __init__(self, limits: Any, ffmpeg: str | Path | None = None, ffprobe: str | Path | None = None):
        self.limits = limits
        self._ffmpeg_arg = ffmpeg
        self._ffprobe_arg = ffprobe

    def check_available(self) -> None:
        resolve_tool('ffmpeg', self._ffmpeg_arg)
        resolve_tool('ffprobe', self._ffprobe_arg)

    async def _run(self, args: list[str], deadline: float, output: Path | None = None) -> bytes:
        if time.monotonic() >= deadline:
            raise AudioError('audio_timeout', 'Audio processing deadline exceeded')
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}),
            )
        except (OSError, ValueError) as exc:
            raise AudioError('audio_tool_start_failed') from None

        async def read_bounded(stream, cap):
            content = bytearray()
            while chunk := await stream.read(8192):
                content.extend(chunk)
                if len(content) > cap:
                    raise AudioError('audio_tool_output_limit')
            return bytes(content)

        async def monitor_size():
            while True:
                if output is not None:
                    try:
                        if output.stat().st_size > self.limits.max_audio_bytes:
                            raise AudioError('audio_size_limit')
                    except FileNotFoundError:
                        pass
                await asyncio.sleep(0.05)

        readers = [asyncio.create_task(read_bounded(process.stdout, 65536)),
                   asyncio.create_task(read_bounded(process.stderr, 16384))]
        waiter = asyncio.create_task(process.wait())
        watchdog = asyncio.create_task(monitor_size())
        tasks = [*readers, waiter, watchdog]
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                # FIRST_COMPLETED lets output-size/read limits kill the child immediately.
                remaining = set(tasks)
                while waiter in remaining:
                    done, _ = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                        remaining.remove(task)
                stdout, _stderr = await asyncio.gather(*readers)
                if process.returncode != 0:
                    raise AudioError('audio_tool_failed')
                if output is not None and output.stat().st_size > self.limits.max_audio_bytes:
                    raise AudioError('audio_size_limit')
                return stdout
        except TimeoutError:
            raise AudioError('audio_timeout', 'Audio processing deadline exceeded') from None
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self, source: Path, deadline: float) -> dict:
        data = await self._run([
            str(resolve_tool('ffprobe', self._ffprobe_arg)), '-v', 'error',
            '-protocol_whitelist', 'file,pipe', '-select_streams', 'a',
            '-show_entries', 'stream=codec_name,sample_rate,channels:format=duration,format_name',
            '-of', 'json', str(source),
        ], deadline)
        try:
            value = json.loads(data)
            streams = value['streams']
            if len(streams) != 1:
                raise ValueError()
            duration = float(value['format']['duration'])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError()
            if duration > self.limits.max_audio_seconds:
                raise AudioError('audio_duration_limit')
            if int(streams[0]['channels']) not in (1, 2):
                raise ValueError()
            if not 8000 <= int(streams[0]['sample_rate']) <= 192000:
                raise ValueError()
            return value
        except (ValueError, TypeError, KeyError, IndexError):
            raise AudioError('invalid_audio') from None

    async def convert(self, source: Path, destination: Path, response_format: str, deadline: float) -> AudioResult:
        source = Path(source).resolve(strict=True)
        destination = Path(destination).resolve()
        if response_format not in ('wav', 'mp3'):
            raise AudioError('unsupported_audio_format')
        if source.parent != destination.parent or source == destination or destination.exists():
            raise AudioError('unsafe_audio_destination')
        if source.stat().st_size <= 0 or source.stat().st_size > self.limits.max_audio_bytes:
            raise AudioError('audio_size_limit')
        # Reject playlist/text formats before a decoder can follow nested file references.
        with source.open('rb') as stream:
            signature = stream.read(16)
        if not (signature.startswith((b'fLaC', b'RIFF', b'OggS', b'ID3')) or
                (len(signature) >= 2 and signature[0] == 0xff and signature[1] & 0xe0 == 0xe0)):
            raise AudioError('invalid_audio_container')
        try:
            free = shutil.disk_usage(destination.parent).free
            if free < 2 * self.limits.max_audio_bytes:
                raise AudioError('audio_disk_space')
        except OSError:
            raise AudioError('audio_disk_space') from None
        stop = min(deadline, time.monotonic() + self.limits.transcode_timeout)
        await self._probe(source, stop)
        codec = ['-c:a', 'pcm_s16le', '-f', 'wav'] if response_format == 'wav' else ['-c:a', 'libmp3lame', '-b:a', '128k', '-f', 'mp3']
        args = [str(resolve_tool('ffmpeg', self._ffmpeg_arg)), '-nostdin', '-v', 'error', '-n',
                '-protocol_whitelist', 'file,pipe', '-i', str(source), '-map', '0:a:0', '-vn',
                '-map_metadata', '-1', '-map_chapters', '-1', '-ac', '1', '-ar', '24000',
                *codec, str(destination)]
        try:
            await self._run(args, stop, destination)
            probe = await self._probe(destination, stop)
            audio = probe['streams'][0]
            if (audio['codec_name'] != ('pcm_s16le' if response_format == 'wav' else 'mp3') or
                    int(audio['sample_rate']) != 24000 or int(audio['channels']) != 1):
                raise AudioError('audio_output_invalid')
            if destination.stat().st_size == 0:
                raise AudioError('audio_output_invalid')
            return AudioResult(destination, 'audio/wav' if response_format == 'wav' else 'audio/mpeg',
                               _owned_paths=(destination, source), _owned_dir=destination.parent)
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
