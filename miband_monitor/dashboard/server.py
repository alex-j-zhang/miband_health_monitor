"""Tiny stdlib asyncio HTTP server exposing the dashboard and telemetry API.

Deliberately stdlib-only (asyncio HTTP server + Server-Sent Events), so the
dashboard works on a minimal install and never competes with the BLE stack for
dependencies. It binds to 127.0.0.1 by default; pass --web-host 0.0.0.0 to
expose it on the LAN.

Endpoints
---------
GET /                 self-contained dashboard page (assets composed inline)
GET /api/snapshot     latest values + recent series + counters (JSON)
GET /api/history      one series: ?kind=heart_rate&limit=600 (JSON);
                      add source=db to read SQLite history instead
GET /events           Server-Sent Events stream of live samples
GET /healthz          liveness probe (JSON)
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import parse_qs

from .constants import (
    HTTP_HEADER_TIMEOUT,
    HTTP_READ_TIMEOUT,
    HTTP_STOP_TIMEOUT,
    METRIC_KINDS,
    SNAPSHOT_POINTS,
    SSE_KEEPALIVE_SECONDS,
)
from .http import send_json, send_response, send_sse, send_sse_headers
from .hub import TelemetryHub
from .page import fallback_index_bytes, page_bytes

logger = logging.getLogger("MiBandDashboard")


class DashboardServer:
    """Tiny asyncio HTTP server exposing the dashboard and telemetry API."""

    def __init__(
        self,
        hub: TelemetryHub,
        host: str = "127.0.0.1",
        port: int = 8080,
        index_path: Optional[str | Path] = None,
        history_provider: Optional[Callable[[str, int], List[dict]]] = None,
    ) -> None:
        self.hub = hub
        self.host = host
        self._requested_port = int(port)
        #: None means: serve the page composed from miband_monitor/dashboard/assets.
        #: A path is honoured as an override (missing/unreadable -> fallback page).
        self.index_path = Path(index_path) if index_path is not None else None
        self.history_provider = history_provider
        self._server: Optional[asyncio.AbstractServer] = None
        self._port = self._requested_port
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        """Actual bound port (useful when constructed with port=0)."""
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
        # explicitly instead of letting wait_closed wait for browsers.
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
            await asyncio.wait_for(server.wait_closed(), timeout=HTTP_STOP_TIMEOUT)
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
            request_line = await asyncio.wait_for(reader.readline(), timeout=HTTP_READ_TIMEOUT)
            if not request_line:
                return
            try:
                method, target, _version = request_line.decode("latin-1").strip().split(" ")
            except ValueError:
                await send_json(writer, HTTPStatus.BAD_REQUEST, {"error": "malformed request"})
                return

            while True:  # drain headers
                header = await asyncio.wait_for(reader.readline(), timeout=HTTP_HEADER_TIMEOUT)
                if header in (b"\r\n", b"\n", b""):
                    break

            path, _, query = target.partition("?")
            params = parse_qs(query)

            if method != "GET":
                await send_json(writer, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "GET only"})
                return

            if path in ("/", "/index.html"):
                await self._serve_index(writer)
            elif path == "/api/snapshot":
                await send_json(writer, HTTPStatus.OK, self.hub.snapshot())
            elif path == "/api/history":
                await self._serve_history(writer, params)
            elif path == "/events":
                await self._serve_sse(writer)
            elif path == "/healthz":
                await send_json(
                    writer,
                    HTTPStatus.OK,
                    {"status": "ok", "subscribers": self.hub.subscriber_count},
                )
            else:
                await send_json(writer, HTTPStatus.NOT_FOUND, {"error": "not found"})
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

    def _index_bytes(self) -> bytes:
        """Read the configured override page, or use the composed assets page."""
        if self.index_path is None:
            return page_bytes()
        return self.index_path.read_bytes()

    async def _serve_index(self, writer: asyncio.StreamWriter) -> None:
        try:
            body = self._index_bytes()
        except OSError:
            body = fallback_index_bytes()
        await send_response(writer, HTTPStatus.OK, "text/html; charset=utf-8", body)

    async def _serve_history(
        self, writer: asyncio.StreamWriter, params: dict[str, list[str]]
    ) -> None:
        kind = (params.get("kind") or ["heart_rate"])[0]
        try:
            limit = int((params.get("limit") or [str(SNAPSHOT_POINTS)])[0])
        except ValueError:
            await send_json(writer, HTTPStatus.BAD_REQUEST, {"error": "invalid limit"})
            return
        if kind not in METRIC_KINDS:
            await send_json(
                writer, HTTPStatus.BAD_REQUEST, {"error": f"unknown kind {kind!r}"}
            )
            return
        limit = max(1, min(limit, self.hub.max_points))
        if self.history_provider is not None and (params.get("source") or ["memory"])[0] == "db":
            try:
                # SQLite I/O must not block the asyncio loop that also serves BLE.
                rows = await asyncio.to_thread(self.history_provider, kind, limit)
            except Exception as exc:
                await send_json(writer, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
        else:
            rows = self.hub.history(kind, limit)
        await send_json(
            writer, HTTPStatus.OK, {"kind": kind, "count": len(rows), "rows": rows}
        )

    async def _serve_sse(self, writer: asyncio.StreamWriter) -> None:
        await send_sse_headers(writer)
        await send_sse(writer, self.hub.snapshot(limit=120))

        queue = self.hub.subscribe()
        try:
            while True:
                try:
                    record = await asyncio.wait_for(
                        queue.get(), timeout=SSE_KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    writer.write(b": keep-alive\n\n")
                    await writer.drain()
                    continue
                await send_sse(writer, record)
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self.hub.unsubscribe(queue)


__all__ = ["DashboardServer"]
