from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from ai_gis_qgis.backend.sam3.client import Sam3Client
from ai_gis_qgis.backend.sam3.errors import Sam3Error


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_GET(self):
        payload = json.dumps(
            {"status": "ok", "model_loaded": True, "version": "0.1.0", "device": "cpu"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            type(self).requests.append(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            payload = b"fake-geotiff"
            self.send_response(200)
            self.send_header("Content-Type", "image/tiff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format, *args):
        return


@pytest.fixture
def sam3_server():
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_health_and_streaming_text_upload(sam3_server: str, tmp_path: Path):
    source = tmp_path / "source.tif"
    source.write_bytes(b"image-bytes")
    target = tmp_path / "mask.tif"
    client = Sam3Client(
        {
            "base_url": sam3_server,
            "api_token": "secret",
            "connect_timeout_seconds": 2,
            "request_timeout_seconds": 2,
        }
    )

    assert client.health()["model_loaded"] is True
    result = client.segment_text(source, target, prompt="building", min_size=4)

    assert target.read_bytes() == b"fake-geotiff"
    assert result["bytes"] == len(b"fake-geotiff")
    request = _Handler.requests[0]
    assert request["path"] == "/segment/text"
    assert request["authorization"] == "Bearer secret"
    assert b'name="prompt"' in request["body"]
    assert b"building" in request["body"]
    assert b"image-bytes" in request["body"]


def test_cancelled_upload_does_not_leave_partial_file(
    sam3_server: str, tmp_path: Path
):
    source = tmp_path / "source.tif"
    source.write_bytes(b"image")
    target = tmp_path / "mask.tif"
    client = Sam3Client(
        {"base_url": sam3_server, "request_timeout_seconds": 2},
        should_cancel=lambda: True,
    )

    with pytest.raises(Sam3Error, match="已取消"):
        client.segment_automatic(source, target)

    assert not target.exists()
