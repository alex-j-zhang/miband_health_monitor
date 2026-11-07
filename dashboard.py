"""Real-time web dashboard and HTTP/SSE API for Mi Band 6 telemetry.

Deliberately **stdlib-only** (asyncio HTTP server + Server-Sent Events), so the
dashboard works on a minimal install and never competes with the BLE stack for
dependencies. It binds to ``127.0.0.1`` by default; pass ``--web-host 0.0.0.0``
to expose it on the LAN.

Endpoints
---------
``GET /``                 self-contained dashboard page (``dashboard.html``)
``GET /api/snapshot``     latest values + recent series + counters (JSON)
``GET /api/history``      one series: ``?kind=heart_rate&limit=600`` (JSON)
``GET /events``           Server-Sent Events stream of live samples
``GET /healthz``          liveness probe (JSON)

The hub is safe to feed from BLE notification callbacks: it only appends to
in-memory ring buffers and fans out to subscriber queues; slow subscribers are
dropped rather than blocking the BLE event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from http import HTTPStatus
from pathlib import Path
from typing import Callable, Deque, Dict, Optional
from urllib.parse import parse_qs

from storage import utc_iso

logger = logging.getLogger("MiBandDashboard")

METRIC_KINDS = ("heart_rate", "steps", "imu", "spo2")
DEFAULT_MAX_POINTS = 7200
DEFAULT_SNAPSHOT_POINTS = 600
SSE_QUEUE_SIZE = 512
SSE_KEEPALIVE_SECONDS = 15.0


class TelemetryHub:
    """Thread-safe ring buffers plus live SSE fan-out."""

    def __init__(self, max_points: int = DEFAULT_MAX_POINTS) -> None:
        self._max_points = max(1, int(max_points))
        self._series: Dict[str, Deque[dict]] = {
            kind: deque(maxlen=self._max_points) for kind in METRIC_KINDS
        }
        self._latest: Dict[str, dict] = {}
        self._counts: Dict[str, int] = {kind: 0 for kind in METRIC_KINDS}
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._events_sent = 0

    # ------------------------------------------------------------- lifecycle
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop so callbacks from other threads can fan out."""
        self._loop = loop

    # --------------------------------------------------------------- publish
    def publish_heart_rate(self, bpm: int, timestamp: Optional[float] = None) -> None:
        self._publish("heart_rate", {"bpm": int(bpm)}, timestamp)

    def publish_steps(self, info: dict, timestamp: Optional[float] = None) -> None:
        self._publish(
            "steps",
            {
                "steps": int(info.get("steps", 0)),
                "distance_m": int(info.get("distance_meters", 0)),
                "calories": int(info.get("calories", 0)),
            },
            timestamp,
        )

    def publish_imu(self, info: dict, timestamp: Optional[float] = None) -> None:
        """Publish one dashboard-friendly aggregate per raw IMU packet."""
        samples = info.get("samples") or []
        if not samples:
            return
        magnitudes = [
            math.hypot(
                float(sample.get("ax_g", 0.0)),
                float(sample.get("ay_g", 0.0)),
                float(sample.get("az_g", 0.0)),
            )
            for sample in samples
        ]
        first = samples[0]
        self._publish(
            "imu",
            {
                "sensor_type": int(info.get("type", 0)),
                "sequence": int(info.get("sequence", 0)),
                "samples": len(samples),
                "ax_g": float(first.get("ax_g", 0.0)),
                "ay_g": float(first.get("ay_g", 0.0)),
                "az_g": float(first.get("az_g", 0.0)),
                "magnitude_g": round(sum(magnitudes) / len(magnitudes), 4),
            },
            timestamp,
        )

    def publish_spo2(self, spo2: int, status: int = 0, timestamp: Optional[float] = None) -> None:
        self._publish("spo2", {"spo2": int(spo2), "status": int(status)}, timestamp)

    def _publish(self, kind: str, payload: dict, timestamp: Optional[float]) -> None:
        event_ts = time.time() if timestamp is None else float(timestamp)
        record = {"kind": kind, "ts": round(event_ts, 6), "iso_time": utc_iso(event_ts), **payload}
        self._series[kind].append(record)  # deque append is atomic under the GIL
        self._latest[kind] = record
        self._counts[kind] += 1

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._fanout, record)
                return
            except RuntimeError:  # loop closed during shutdown
                pass
        self._fanout(record)

    def _fanout(self, record: dict) -> None:
        self._events_sent += 1
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                logger.debug("Dropping dashboard event: SSE subscriber is too slow")

    # ------------------------------------------------------------- consumers
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def snapshot(self, limit: int = DEFAULT_SNAPSHOT_POINTS) -> dict:
        limit = max(0, int(limit))
        series = {
            kind: list(buffer)[-limit:] if limit else []
            for kind, buffer in self._series.items()
        }
        return {
            "type": "snapshot",
            "server_time": round(time.time(), 6),
            "counts": dict(self._counts),
            "latest": dict(self._latest),
            "series": series,
            "subscribers": len(self._subscribers),
            "events_sent": self._events_sent,
        }

    def history(self, kind: str, limit: int = DEFAULT_SNAPSHOT_POINTS) -> list[dict]:
        if kind not in METRIC_KINDS:
            raise ValueError(f"unknown metric kind: {kind!r}")
        buffer = self._series[kind]
        limit = max(0, int(limit))
        return list(buffer)[-limit:] if limit else []


class DashboardServer:
    """Tiny asyncio HTTP server exposing the dashboard and telemetry API."""

    def __init__(
        self,
        hub: TelemetryHub,
        host: str = "127.0.0.1",
        port: int = 8080,
        index_path: Optional[str | Path] = None,
        history_provider: Optional[Callable[[str, int], list[dict]]] = None,
    ) -> None:
        self.hub = hub
        self.host = host
        self._requested_port = int(port)
        self.index_path = (
            Path(index_path) if index_path is not None else Path(__file__).with_name("dashboard.html")
        )
        self.history_provider = history_provider
        self._server: Optional[asyncio.AbstractServer] = None
        self._port = self._requested_port
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        """Actual bound port (useful when constructed with ``port=0``)."""
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self._port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client_task, self.host, self._requested_port
        )
        sockets = self._server.sockets or ()
        if sockets:
            self._port = sockets[0].getsockname()[1]
        logger.info("Dashboard listening on %s", self.url)

    async def stop(self) -> None:
        if self._server is None:
            return
        server = self._server
        self._server = None
        server.close()

        # SSE handlers block until their next keep-alive tick, so cancel them
        # explicitly instead of letting ``wait_closed`` wait for browsers.
        current_task = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current_task:
                task.cancel()
        for writer in list(self._writers):
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass

        try:
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.debug("Timed out waiting for dashboard connections to close")

        pending = [task for task in self._tasks if task is not current_task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._writers.clear()
        logger.info("Dashboard stopped.")

    async def _handle_client_task(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        try:
            await self._handle_client(reader, writer)
        finally:
            self._tasks.discard(task)
            self._writers.discard(writer)

    # ------------------------------------------------------------ HTTP glue
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not request_line:
                return
            try:
                method, target, _version = request_line.decode("latin-1").strip().split(" ")
            except ValueError:
                await self._send_json(writer, HTTPStatus.BAD_REQUEST, {"error": "malformed request"})
                return

            while True:  # drain headers
                header = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if header in (b"\r\n", b"\n", b""):
                    break

            path, _, query = target.partition("?")
            params = parse_qs(query)

            if method != "GET":
                await self._send_json(writer, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "GET only"})
                return

            if path in ("/", "/index.html"):
                await self._serve_index(writer)
            elif path == "/api/snapshot":
                await self._send_json(writer, HTTPStatus.OK, self.hub.snapshot())
            elif path == "/api/history":
                await self._serve_history(writer, params)
            elif path == "/events":
                await self._serve_sse(writer)
            elif path == "/healthz":
                await self._send_json(
                    writer,
                    HTTPStatus.OK,
                    {"status": "ok", "subscribers": self.hub.subscriber_count},
                )
            else:
                await self._send_json(writer, HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unhandled dashboard request error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _serve_index(self, writer: asyncio.StreamWriter) -> None:
        try:
            body = self.index_path.read_bytes()
        except OSError:
            body = _FALLBACK_INDEX.encode("utf-8")
        await self._send_response(writer, HTTPStatus.OK, "text/html; charset=utf-8", body)

    async def _serve_history(
        self, writer: asyncio.StreamWriter, params: dict[str, list[str]]
    ) -> None:
        kind = (params.get("kind") or ["heart_rate"])[0]
        try:
            limit = int((params.get("limit") or [str(DEFAULT_SNAPSHOT_POINTS)])[0])
        except ValueError:
            await self._send_json(writer, HTTPStatus.BAD_REQUEST, {"error": "invalid limit"})
            return
        if kind not in METRIC_KINDS:
            await self._send_json(
                writer, HTTPStatus.BAD_REQUEST, {"error": f"unknown kind {kind!r}"}
            )
            return
        limit = max(1, min(limit, self.hub._max_points))
        if self.history_provider is not None and (params.get("source") or ["memory"])[0] == "db":
            try:
                rows = self.history_provider(kind, limit)
            except Exception as exc:
                await self._send_json(writer, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
        else:
            rows = self.hub.history(kind, limit)
        await self._send_json(writer, HTTPStatus.OK, {"kind": kind, "count": len(rows), "rows": rows})

    async def _serve_sse(self, writer: asyncio.StreamWriter) -> None:
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "X-Accel-Buffering: no\r\n"
            "\r\n"
        )
        writer.write(headers.encode("latin-1"))
        await writer.drain()
        await self._send_sse(writer, self.hub.snapshot(limit=120))

        queue = self.hub.subscribe()
        try:
            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    writer.write(b": keep-alive\n\n")
                    await writer.drain()
                    continue
                await self._send_sse(writer, record)
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self.hub.unsubscribe(queue)

    @staticmethod
    async def _send_sse(writer: asyncio.StreamWriter, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        writer.write(f"data: {data}\n\n".encode("utf-8"))
        await writer.drain()

    async def _send_json(
        self, writer: asyncio.StreamWriter, status: HTTPStatus, payload: dict
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._send_response(writer, status, "application/json; charset=utf-8", body)

    @staticmethod
    async def _send_response(
        writer: asyncio.StreamWriter,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
    ) -> None:
        head = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()


_FALLBACK_INDEX = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Mi Band 6 实时看板</title></head>
<body style="font-family:sans-serif;background:#0f1216;color:#e6edf3;padding:24px">
<h1>Mi Band 6 实时看板</h1>
<p>dashboard.html 未找到，但 API 仍可用：</p>
<ul><li><a style="color:#38bdf8" href="/api/snapshot">/api/snapshot</a></li>
<li><a style="color:#38bdf8" href="/events">/events</a> (SSE)</li>
<li><a style="color:#38bdf8" href="/healthz">/healthz</a></li></ul>
</body></html>"""
