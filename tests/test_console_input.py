"""Real pipe/file boundaries: command selectors must not become confirmations."""
import os

import pytest

from comfybridge.console_input import ConsoleInput


@pytest.fixture
def pipe():
    read_fd, write_fd = os.pipe()
    reader_file = os.fdopen(read_fd, 'rb', buffering=0)
    closed = False
    def close_writer():
        nonlocal closed
        if not closed:
            os.close(write_fd)
            closed = True
    try:
        yield ConsoleInput(stream=reader_file, prompt_writer=lambda _: None), write_fd, close_writer
    finally:
        close_writer()
        reader_file.close()


def test_pipe_read_does_not_prefetch_monitor_or_confirmation(pipe):
    reader, writer, close = pipe
    os.write(writer, b'1\n2\n1\n0\n0\n')
    close()
    assert reader.ask('home') == '1'
    assert reader.poll_key() == '2'
    assert reader.ask('confirm') == '1'
    assert reader.poll_key() == '0'
    assert reader.ask('home') == '0'
    with pytest.raises(EOFError):
        reader.ask('home')


def test_open_empty_pipe_poll_does_not_block(pipe):
    reader, writer, close = pipe
    assert reader.poll_key() is None
    os.write(writer, b'3')
    assert reader.poll_key() is None  # A redirected command is not complete yet.
    os.write(writer, b'\n1\n')
    assert reader.poll_key() == '3'
    assert reader.ask('confirm') == '1'
    assert reader.poll_key() is None
    close()
    assert reader.poll_key() == 'q'


def test_utf8_secret_and_following_commands_are_not_echoed_or_prefetched(pipe):
    reader, writer, close = pipe
    prompts = []
    reader.prompt_writer = prompts.append
    os.write(writer, 'my-secret-key\n中文 路径.json\nq\n'.encode())
    close()
    assert reader.secret('Key: ') == 'my-secret-key'
    assert reader.ask('Path: ') == '中文 路径.json'
    assert reader.poll_key() == 'q'
    assert prompts == ['Key: ', 'Path: ']


@pytest.mark.parametrize('payload', [b'q', b'Q\r\n', b'0\n'])
def test_partial_eof_and_crlf_commands(pipe, payload):
    reader, writer, close = pipe
    os.write(writer, payload)
    close()
    assert reader.poll_key() in ('q', '0')
    assert reader.poll_key() == 'q'


def test_long_invalid_command_is_discarded_not_truncated_into_action(pipe):
    reader, writer, close = pipe
    os.write(writer, b'3' * 100 + b'\n0\n')
    close()
    assert reader.poll_key() is None
    assert reader.poll_key() == '0'


def test_regular_file_commands_and_unicode(tmp_path):
    source = tmp_path / 'input.txt'
    source.write_bytes('a\n1\n新的音色\n0'.encode())
    with source.open('rb', buffering=0) as stream:
        reader = ConsoleInput(stream=stream, prompt_writer=lambda _: None)
        assert reader.ask('menu') == 'a'
        assert reader.poll_key() == '1'
        assert reader.ask('voice') == '新的音色'
        assert reader.poll_key() == '0'
        assert reader.poll_key() == 'q'
