from __future__ import annotations

import asyncio
import io
import os
import stat
import tempfile
from types import SimpleNamespace
from wsgiref.simple_server import demo_app

import pytest
from asgiref.wsgi import WsgiToAsgi

from tests.test_whitenoise import files  # noqa: F401
from whitenoise.asgi import (
    AsgiWhiteNoise,
    convert_asgi_headers,
    convert_wsgi_headers,
    read_file,
)
from whitenoise.responders import StaticFile


@pytest.fixture()
def loop():
    return asyncio.new_event_loop()


@pytest.fixture()
def static_file_sample():
    content = b"01234567890123456789"
    modification_time = "Sun, 09 Sep 2001 01:46:40 GMT"
    modification_epoch = 1000000000
    temporary_file = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
    try:
        temporary_file.write(content)
        temporary_file.close()
        stat_cache = {
            temporary_file.name: SimpleNamespace(
                st_mode=stat.S_IFREG, st_size=len(content), st_mtime=modification_epoch
            )
        }
        static_file = StaticFile(temporary_file.name, [], stat_cache=stat_cache)
        yield {
            "static_file": static_file,
            "content": content,
            "content_length": len(content),
            "modification_time": modification_time,
        }
    finally:
        os.unlink(temporary_file.name)


@pytest.fixture(params=["GET", "HEAD"])
def method(request):
    return request.param


@pytest.fixture(params=[10, 20])
def block_size(request):
    return request.param


@pytest.fixture()
def file_not_found():
    async def application(scope, receive, send):
        if scope["type"] != "http":
            raise RuntimeError()
        await receive()
        await send({"type": "http.response.start", "status": 404})
        await send({"type": "http.response.body", "body": b"Not found"})

    return application


@pytest.fixture()
def websocket():
    async def application(scope, receive, send):
        if scope["type"] != "websocket":
            raise RuntimeError()
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close"})

    return application


class Receiver:
    def __init__(self):
        self.events = [{"type": "http.request"}]

    async def __call__(self):
        return self.events.pop(0)


class Sender:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


@pytest.fixture()
def receive():
    return Receiver()


@pytest.fixture()
def send():
    return Sender()


@pytest.fixture(params=[True, False], scope="module")
def application(request, files):  # noqa: F811

    return AsgiWhiteNoise(
        WsgiToAsgi(demo_app),
        root=files.directory,
        max_age=1000,
        mimetypes={".foobar": "application/x-foo-bar"},
        index_file=True,
    )


def test_asgiwhitenoise(loop, receive, send, method, application, files):  # noqa: F811
    scope = {
        "type": "http",
        "path": "/" + files.js_path,
        "headers": [],
        "method": method,
    }
    loop.run_until_complete(application(scope, receive, send))
    assert receive.events == []
    assert send.events[0]["status"] == 200
    if method == "GET":
        assert send.events[1]["body"] == files.js_content


def test_serve_static_file(loop, send, method, block_size, static_file_sample):
    loop.run_until_complete(
        AsgiWhiteNoise.serve(
            send, static_file_sample["static_file"], method, {}, block_size
        )
    )
    expected_events = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"last-modified", static_file_sample["modification_time"].encode()),
                (b"etag", static_file_sample["static_file"].etag.encode()),
                (b"content-length", str(static_file_sample["content_length"]).encode()),
            ],
        }
    ]
    if method == "GET":
        for start in range(0, static_file_sample["content_length"], block_size):
            expected_events.append(
                {
                    "type": "http.response.body",
                    "body": static_file_sample["content"][start : start + block_size],
                    "more_body": True,
                }
            )
    expected_events.append({"type": "http.response.body"})
    assert send.events == expected_events


def test_receive_request(loop, receive):
    loop.run_until_complete(AsgiWhiteNoise.receive(receive))
    assert receive.events == []


def test_receive_request_with_more_body(loop, receive):
    receive.events = [
        {"type": "http.request", "more_body": True, "body": b"content"},
        {"type": "http.request", "more_body": True, "body": b"more content"},
        {"type": "http.request"},
    ]
    loop.run_until_complete(AsgiWhiteNoise.receive(receive))
    assert not receive.events


def test_receive_request_with_invalid_event(loop, receive):
    receive.events = [{"type": "http.weirdstuff"}]
    with pytest.raises(RuntimeError):
        loop.run_until_complete(AsgiWhiteNoise.receive(receive))


def test_read_file():
    content = io.BytesIO(b"0123456789")
    content.seek(4)
    blocks = list(read_file(content, content_length=5, block_size=2))
    assert blocks == [b"45", b"67", b"8"]


def test_read_too_short_file():
    content = io.BytesIO(b"0123456789")
    content.seek(4)
    with pytest.raises(RuntimeError):
        list(read_file(content, content_length=11, block_size=2))


def test_convert_asgi_headers():
    wsgi_headers = convert_asgi_headers(
        [
            (b"accept-encoding", b"gzip,br"),
            (b"range", b"bytes=10-100"),
        ]
    )
    assert wsgi_headers == {
        "HTTP_ACCEPT_ENCODING": "gzip,br",
        "HTTP_RANGE": "bytes=10-100",
    }


def test_convert_wsgi_headers():
    wsgi_headers = convert_wsgi_headers(
        [
            ("Content-Length", "1234"),
            ("ETag", "ada"),
        ]
    )
    assert wsgi_headers == [
        (b"content-length", b"1234"),
        (b"etag", b"ada"),
    ]
