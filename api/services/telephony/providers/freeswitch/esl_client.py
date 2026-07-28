"""Minimal async FreeSWITCH Event Socket Library (ESL) client.

Implements only inbound-mode ESL (we connect out to FreeSWITCH's
mod_event_socket) and only the commands this provider needs: auth,
api/bgapi request-reply, and event subscription. Deliberately narrow rather
than a full-featured ESL library — see providers/freeswitch/DESIGN.md for why.

Wire protocol: line-based headers terminated by a blank line, optionally
followed by a Content-Length-sized body, mirroring the framing FreeSWITCH's
mod_event_socket uses for both command replies and events. Reference framing
behavior confirmed against the unmerged api/services/telephony/upstream_pbx.py
POC (commit bd77a5a6 on origin/feat/external-pbx), rewritten here as a
reusable, testable client instead of a one-off inline function.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, Optional

from loguru import logger

_AUTH_REQUEST_HEADER = "Content-Type: auth/request"

# Sentinel distinguishing "use the constructor's default timeout" from an
# explicit `timeout=None` ("wait indefinitely") in _read_frame's signature.
_UNSET = object()


@dataclass
class ESLReply:
    """Parsed response to an `api`/`bgapi` command."""

    headers: Dict[str, str]
    body: str

    @property
    def ok(self) -> bool:
        return self.body.startswith("+OK") or self.headers.get(
            "Reply-Text", ""
        ).startswith("+OK")

    @property
    def error_text(self) -> str:
        if self.body.startswith("-ERR"):
            return self.body
        return self.headers.get("Reply-Text", "")


@dataclass
class ESLEvent:
    """A single FreeSWITCH event, parsed from a `json` event body."""

    headers: Dict[str, str]
    body: str = ""
    data: dict = field(default_factory=dict)

    def get(self, key: str, default=None):
        return self.data.get(key, default)


class ESLAuthError(Exception):
    """Raised when the ESL password is rejected."""


class ESLConnectionClosed(Exception):
    """Raised when the socket closes while reading a frame."""


class ESLTransport:
    """A single inbound-mode ESL connection.

    Narrow, mockable interface: ``connect``/``api``/``bgapi``/``events``.
    Tests fake this class at the request/reply boundary rather than the raw
    socket, so call-logic tests don't depend on ESL wire-framing details.
    """

    def __init__(self, host: str, port: int, password: str, *, timeout: float = 8.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open the socket and authenticate. Raises ESLAuthError on bad password."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        self._reader, self._writer = reader, writer
        try:
            headers, _ = await self._read_frame()
            if headers.get("Content-Type") != "auth/request":
                raise ESLAuthError(
                    f"Unexpected greeting from {self.host}:{self.port}: {headers}"
                )
            reply = await self._send_and_read(f"auth {self.password}")
            if not reply.ok:
                raise ESLAuthError(f"FreeSWITCH ESL auth rejected: {reply.error_text}")
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def subscribe(self, *event_names: str) -> ESLReply:
        """Subscribe to the given event names in JSON format (`event json ...`)."""
        names = " ".join(event_names) if event_names else "ALL"
        return await self.api(f"event json {names}", command_prefix="")

    async def api(self, command: str, *, command_prefix: str = "api ") -> ESLReply:
        """Send a blocking `api <command>` and return its reply."""
        async with self._lock:
            return await self._send_and_read(f"{command_prefix}{command}")

    async def bgapi(self, command: str) -> ESLReply:
        """Send a non-blocking `bgapi <command>`.

        FreeSWITCH acknowledges immediately with a Job-UUID; the actual
        command result arrives later as a BACKGROUND_JOB event, which
        callers observe via :meth:`events` rather than this return value.
        """
        async with self._lock:
            return await self._send_and_read(f"bgapi {command}")

    async def events(self) -> AsyncIterator[ESLEvent]:
        """Yield events as they arrive. Raises ESLConnectionClosed when the
        socket closes so the caller's reconnect loop can react.

        Waits indefinitely between events (``timeout=None``) — unlike a
        command round-trip, there's no way to know how long until the next
        real FreeSWITCH event arrives, and applying the constructor's
        command-reply timeout here would spuriously "time out" (and trigger
        a reconnect) during any idle period longer than it, even though the
        connection is perfectly healthy.
        """
        while True:
            headers, body = await self._read_frame(timeout=None)
            content_type = headers.get("Content-Type", "")
            if content_type in ("auth/request",):
                continue
            if content_type == "command/reply" or content_type == "api/response":
                # A reply arriving on the event stream (shouldn't normally
                # happen since api/bgapi hold the lock while reading their
                # own reply) — log and skip rather than misinterpret it.
                logger.debug(f"[ESL] Unexpected reply on event stream: {headers}")
                continue
            yield _parse_event(headers, body)

    async def _send_and_read(self, command: str) -> ESLReply:
        if self._writer is None:
            raise ESLConnectionClosed("ESL socket not connected")
        self._writer.write(f"{command}\n\n".encode())
        await self._writer.drain()
        headers, body = await self._read_frame()
        return ESLReply(headers=headers, body=body)

    async def _read_frame(
        self, *, timeout: "Optional[float] | object" = _UNSET
    ) -> tuple[Dict[str, str], str]:
        """Read one header block (terminated by a blank line) plus any
        Content-Length-declared body.

        ``timeout`` defaults to the constructor's command-reply timeout;
        pass ``None`` explicitly to wait indefinitely (used by :meth:`events`
        for the next-event read, where no fixed deadline is meaningful).
        """
        if self._reader is None:
            raise ESLConnectionClosed("ESL socket not connected")
        effective_timeout = self.timeout if timeout is _UNSET else timeout

        async def _read() -> tuple[Dict[str, str], str]:
            raw_headers = await self._reader.readuntil(b"\n\n")
            headers = _parse_headers(raw_headers.decode(errors="replace"))
            length = int(headers.get("Content-Length", "0") or 0)
            body = ""
            if length:
                raw_body = await self._reader.readexactly(length)
                body = raw_body.decode(errors="replace")
            return headers, body

        if effective_timeout is None:
            return await _read()
        return await asyncio.wait_for(_read(), timeout=effective_timeout)


def _parse_headers(raw: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()
    return headers


def _parse_event(headers: Dict[str, str], body: str) -> ESLEvent:
    """Parse an event frame. `event json` subscriptions deliver the event as
    a JSON body regardless of the outer frame's Content-Type."""
    import json

    data: dict = {}
    if body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            logger.debug(f"[ESL] Non-JSON event body, ignoring: {body[:200]}")
    return ESLEvent(headers=headers, body=body, data=data)
