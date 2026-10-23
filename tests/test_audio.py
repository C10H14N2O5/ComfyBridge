from __future__ import annotations

import asyncio
import math
from pathlib import Path
import shutil
import struct
import sys
import time
from types import SimpleNamespace
import wave

import pytest

from comfybridge.audio import AudioError, AudioProcessor, AudioResult, resolve_tool


def limits(**changes):
    values = dict(max_audio_bytes=128 * 1024 * 1024, max_audio_seconds=600.0, transcode_timeout=10.0)
    values.update(changes)
    return SimpleNamespace(**values)


def make_wav(path: Path, seconds=0.2):
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b''.join(struct.pack('<h', int(2000 * math.sin(i / 16000 * math.tau * 440)))
                             for i in range(int(seconds * 16000))))


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='local FFmpeg unavailable')
@pytest.mark.parametrize('fmt', ['wav', 'mp3'])
async def test_real_conversion_and_owned_cleanup(tmp_path, fmt):
    request_dir = tmp_path / '请求 空格'
    request_dir.mkdir()
    source = request_dir / 'source.wav'
    make_wav(source)
    result = await AudioProcessor(limits()).convert(source, request_dir / ('output.' + fmt), fmt, time.monotonic() + 10)
    assert result.path.is_file()
    assert result.media_type == ('audio/wav' if fmt == 'wav' else 'audio/mpeg')
    if fmt == 'wav':
        with wave.open(str(result.path), 'rb') as output:
            assert (output.getnchannels(), output.getframerate(), output.getsampwidth()) == (1, 24000, 2)
    calls = []
    async def release():
        calls.append('released')
    result.cleanup_callback = release
    await result.cleanup()
    await result.cleanup()
    assert calls == ['released']
    assert not request_dir.exists()


async def test_cleanup_does_not_remove_unregistered_files(tmp_path):
    path = tmp_path / 'audio.wav'
    path.write_bytes(b'audio')
    unrelated = tmp_path / 'keep.txt'
    unrelated.write_text('keep')
    result = AudioResult(path, 'audio/wav', _owned_paths=(path,), _owned_dir=tmp_path)
    await result.cleanup()
    assert unrelated.read_text() == 'keep'


async def test_callback_runs_if_file_cleanup_fails(tmp_path, monkeypatch):
    path = tmp_path / 'file'
    path.write_bytes(b'file')
    calls = []
    def denied(*args, **kwargs):
        raise PermissionError()
    monkeypatch.setattr(Path, 'unlink', denied)
    result = AudioResult(path, 'audio/wav', lambda: calls.append(1), _owned_paths=(path,))
    await result.cleanup()
    assert calls == [1]


async def test_source_size_and_format_validation(tmp_path):
    source = tmp_path / 'source'
    source.write_text('not audio')
    with pytest.raises(AudioError, match='Audio processing failed') as error:
        await AudioProcessor(limits()).convert(source, tmp_path / 'out.wav', 'wav', time.monotonic() + 2)
    assert error.value.code == 'invalid_audio_container'
    with pytest.raises(AudioError) as error:
        await AudioProcessor(limits(max_audio_bytes=2)).convert(source, tmp_path / 'out.wav', 'wav', time.monotonic() + 2)
    assert error.value.code == 'audio_size_limit'


async def test_destination_confinement_and_no_overwrite(tmp_path):
    source = tmp_path / 'source.wav'
    make_wav(source)
    out = tmp_path / 'out.wav'
    out.write_text('existing')
    with pytest.raises(AudioError) as error:
        await AudioProcessor(limits()).convert(source, out, 'wav', time.monotonic() + 5)
    assert error.value.code == 'unsafe_audio_destination'
    assert out.read_text() == 'existing'


async def test_process_timeout_and_bounded_output():
    processor = AudioProcessor(limits())
    with pytest.raises(AudioError) as error:
        await processor._run([sys.executable, '-c', 'import time;time.sleep(10)'], time.monotonic() + 0.2)
    assert error.value.code == 'audio_timeout'
    with pytest.raises(AudioError) as error:
        await processor._run([sys.executable, '-c', "print('x'*100000)"], time.monotonic() + 5)
    assert error.value.code == 'audio_tool_output_limit'


async def test_nonzero_exit_is_sanitized():
    with pytest.raises(AudioError) as error:
        await AudioProcessor(limits())._run([sys.executable, '-c', "import sys;sys.stderr.write('PRIVATE TEXT');sys.exit(2)"], time.monotonic() + 5)
    assert error.value.code == 'audio_tool_failed'
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='local FFprobe unavailable')
async def test_duration_limit(tmp_path):
    source = tmp_path / 'source.wav'
    make_wav(source, 0.3)
    with pytest.raises(AudioError) as error:
        await AudioProcessor(limits(max_audio_seconds=0.1)).convert(source, tmp_path / 'out.wav', 'wav', time.monotonic() + 5)
    assert error.value.code == 'audio_duration_limit'


def test_frozen_build_does_not_fall_back_to_system_tools(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(shutil, 'which', lambda _: '/pretend/system/tool')
    with pytest.raises(AudioError):
        resolve_tool('not-a-real-program')
