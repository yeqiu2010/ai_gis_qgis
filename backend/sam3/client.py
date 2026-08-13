"""Dependency-free streaming client for SAM3-Geo-API."""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import ssl
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .errors import Sam3Error

CancelChecker = Callable[[], bool]
ProgressCallback = Callable[[str, int, int], None]


class Sam3Client:
    def __init__(self, config: dict[str, Any], *, should_cancel: CancelChecker | None = None):
        self.base_url = str(config.get("base_url") or "").strip().rstrip("/")
        self.api_token = str(config.get("api_token") or "").strip()
        self.connect_timeout = int(config.get("connect_timeout_seconds") or 10)
        self.request_timeout = int(config.get("request_timeout_seconds") or 1200)
        self.verify_tls = bool(config.get("verify_tls", True))
        self.should_cancel = should_cancel or (lambda: False)
        self._parsed = urlparse(self.base_url)
        if self._parsed.scheme not in {"http", "https"} or not self._parsed.netloc:
            raise Sam3Error("SAM3 Base URL 无效。", code="invalid_request")

    def health(self) -> dict[str, Any]:
        last_error: Sam3Error | None = None
        for attempt in range(2):
            connection = self._connection(timeout=self.connect_timeout)
            try:
                connection.request("GET", self._path("/health"), headers=self._headers())
                response = connection.getresponse()
                payload = self._read_json_response(response)
                if response.status < 200 or response.status >= 300:
                    raise self._http_error(response.status, payload)
                if not isinstance(payload, dict):
                    raise Sam3Error("SAM3 健康检查返回的不是 JSON object。", code="invalid_response")
                return payload
            except Sam3Error as exc:
                last_error = exc
                if not exc.retryable or attempt:
                    raise
                time.sleep(0.2)
            except (TimeoutError, OSError, http.client.HTTPException) as exc:
                last_error = self._network_error(exc)
                if attempt:
                    raise last_error from exc
                time.sleep(0.2)
            finally:
                connection.close()
        assert last_error is not None
        raise last_error

    def segment_text(
        self,
        image_path: str | Path,
        output_path: str | Path,
        *,
        prompt: str,
        confidence_threshold: float | None = None,
        min_size: int = 0,
        max_size: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "prompt": prompt,
            "output_format": "geotiff",
            "min_size": min_size,
        }
        if confidence_threshold is not None:
            fields["confidence_threshold"] = confidence_threshold
        if max_size is not None:
            fields["max_size"] = max_size
        return self._upload(
            "/segment/text", image_path, output_path, fields=fields, progress=progress
        )

    def segment_automatic(
        self,
        image_path: str | Path,
        output_path: str | Path,
        *,
        unique: bool = True,
        min_size: int = 0,
        max_size: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "output_format": "geotiff",
            "unique": str(bool(unique)).lower(),
            "min_size": min_size,
        }
        if max_size is not None:
            fields["max_size"] = max_size
        return self._upload(
            "/segment/automatic", image_path, output_path, fields=fields, progress=progress
        )

    def segment_boxes(
        self,
        image_path: str | Path,
        output_path: str | Path,
        *,
        boxes: list[list[float]],
        box_crs: str | None = None,
        min_size: int = 0,
        max_size: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "boxes": json.dumps(boxes, separators=(",", ":")),
            "output_format": "geotiff",
            "min_size": min_size,
        }
        if box_crs:
            fields["box_crs"] = box_crs
        if max_size is not None:
            fields["max_size"] = max_size
        return self._upload(
            "/segment/predict", image_path, output_path, fields=fields, progress=progress
        )

    def _upload(
        self,
        endpoint: str,
        image_path: str | Path,
        output_path: str | Path,
        *,
        fields: dict[str, Any],
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        image_path = Path(image_path).resolve()
        output_path = Path(output_path).resolve()
        if not image_path.is_file():
            raise Sam3Error(f"SAM3 请求影像不存在：{image_path}", code="invalid_request")
        boundary = f"----qgis-sam3-{uuid.uuid4().hex}"
        preamble, closing = self._multipart_parts(boundary, fields, image_path)
        total = len(preamble) + image_path.stat().st_size + len(closing)
        connection = self._connection(timeout=self.request_timeout)
        started = time.monotonic()
        try:
            connection.putrequest("POST", self._path(endpoint))
            for key, value in self._headers().items():
                connection.putheader(key, value)
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(total))
            connection.endheaders()
            self._check_cancelled()
            connection.send(preamble)
            sent = len(preamble)
            with image_path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    self._check_cancelled()
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress("upload", sent, total)
            connection.send(closing)
            if progress:
                progress("inference", total, total)
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                payload = self._read_error_payload(response)
                raise self._http_error(response.status, payload)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            part_path = output_path.with_suffix(output_path.suffix + ".part")
            received = 0
            expected = int(response.getheader("Content-Length") or 0)
            try:
                with part_path.open("wb") as handle:
                    while chunk := response.read(1024 * 1024):
                        self._check_cancelled()
                        handle.write(chunk)
                        received += len(chunk)
                        if progress:
                            progress("download", received, expected)
                if received == 0:
                    raise Sam3Error("SAM3 返回了空文件。", code="invalid_response")
                os.replace(part_path, output_path)
            finally:
                if part_path.exists():
                    part_path.unlink()
            return {
                "path": str(output_path),
                "bytes": received,
                "content_type": response.getheader("Content-Type") or "",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        except Sam3Error:
            raise
        except TimeoutError as exc:
            raise Sam3Error(
                f"SAM3 推理请求超时（{self.request_timeout} 秒）。",
                code="inference_timeout",
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise self._network_error(exc) from exc
        finally:
            connection.close()

    def _connection(self, *, timeout: int):
        host = self._parsed.hostname or ""
        if self._parsed.scheme == "https":
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(
                host, self._parsed.port or 443, timeout=timeout, context=context
            )
        return http.client.HTTPConnection(host, self._parsed.port or 80, timeout=timeout)

    def _path(self, endpoint: str) -> str:
        prefix = self._parsed.path.rstrip("/")
        return f"{prefix}/{endpoint.lstrip('/')}" or "/"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, image/tiff"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _multipart_parts(
        self,
        boundary: str,
        fields: dict[str, Any],
        image_path: Path,
    ) -> tuple[bytes, bytes]:
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(
                f'Content-Disposition: form-data; name="{quote(str(name), safe="_-" )}"\r\n\r\n'.encode()
            )
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
        filename = image_path.name.replace('"', "_")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
        return b"".join(chunks), f"\r\n--{boundary}--\r\n".encode()

    def _read_json_response(self, response) -> Any:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise Sam3Error("SAM3 JSON 响应过大。", code="invalid_response")
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Sam3Error("SAM3 返回了无效 JSON。", code="invalid_response") from exc

    def _read_error_payload(self, response) -> Any:
        try:
            return self._read_json_response(response)
        except Sam3Error:
            return {"detail": f"HTTP {response.status}"}

    def _http_error(self, status: int, payload: Any) -> Sam3Error:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        message = str(detail or f"SAM3 服务返回 HTTP {status}")
        mapping = {
            400: ("invalid_request", False),
            401: ("authentication_failed", False),
            403: ("authentication_failed", False),
            404: ("no_objects_found", False),
            413: ("payload_too_large", False),
            422: ("invalid_request", False),
            503: ("model_not_ready", True),
        }
        code, retryable = mapping.get(
            status,
            ("service_unavailable", status in {502, 503, 504}),
        )
        return Sam3Error(message, code=code, status_code=status, retryable=retryable)

    def _network_error(self, exc: Exception) -> Sam3Error:
        return Sam3Error(
            f"无法连接 SAM3 服务：{exc}",
            code="service_unreachable",
            retryable=True,
        )

    def _check_cancelled(self) -> None:
        if self.should_cancel():
            raise Sam3Error("SAM3 请求已取消。", code="cancelled")
