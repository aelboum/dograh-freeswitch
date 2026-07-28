"""Tests for the low-level ESL wire framing itself.

Unlike test_provider.py (which fakes ESLTransport at the request/reply
boundary), this file tests ESLTransport's actual socket framing against a
tiny local TCP server that mimics FreeSWITCH's ESL greeting/auth/api-reply
protocol — this is the one place raw byte framing needs real coverage,
since ESLTransport itself defines that boundary.
"""

import asyncio

import pytest

from api.services.telephony.providers.freeswitch.esl_client import (
    ESLAuthError,
    ESLTransport,
)


async def _run_fake_esl_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, accept_password: str):
    writer.write(b"Content-Type: auth/request\n\n")
    await writer.drain()

    line = await reader.readuntil(b"\n\n")
    auth_command = line.decode().strip()
    authenticated = auth_command == f"auth {accept_password}"

    reply_text = "+OK accepted" if authenticated else "-ERR invalid"
    writer.write(f"Content-Type: command/reply\nReply-Text: {reply_text}\n\n".encode())
    await writer.drain()

    if not authenticated:
        writer.close()
        return

    # Post-auth: handle one api command with a Content-Length body.
    try:
        line = await reader.readuntil(b"\n\n")
    except asyncio.IncompleteReadError:
        writer.close()
        return
    command = line.decode().strip()
    if command.startswith("api "):
        body = b"+OK\n"
        writer.write(
            f"Content-Type: api/response\nContent-Length: {len(body)}\n\n".encode()
            + body
        )
        await writer.drain()
    writer.close()


@pytest.fixture
async def fake_esl_server():
    async def handler(reader, writer):
        await _run_fake_esl_server(reader, writer, accept_password="ClueCon")

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    async with server:
        yield host, port


@pytest.mark.asyncio
async def test_connect_succeeds_with_correct_password(fake_esl_server):
    host, port = fake_esl_server
    transport = ESLTransport(host, port, "ClueCon")
    await transport.connect()
    assert transport.connected is True
    await transport.close()


@pytest.mark.asyncio
async def test_connect_raises_auth_error_with_wrong_password(fake_esl_server):
    host, port = fake_esl_server
    transport = ESLTransport(host, port, "wrong-password")
    with pytest.raises(ESLAuthError):
        await transport.connect()
    assert transport.connected is False


@pytest.mark.asyncio
async def test_api_command_reads_content_length_body(fake_esl_server):
    host, port = fake_esl_server
    transport = ESLTransport(host, port, "ClueCon")
    await transport.connect()
    try:
        reply = await transport.api("status")
        assert reply.ok is True
        assert reply.body == "+OK\n"
    finally:
        await transport.close()


async def _run_slow_event_server(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, delay_seconds: float
):
    """Auth successfully, then stay silent for `delay_seconds` before sending
    one CHANNEL_PARK event — models an idle ESL connection with no call
    activity for longer than a short command-reply timeout."""
    writer.write(b"Content-Type: auth/request\n\n")
    await writer.drain()
    await reader.readuntil(b"\n\n")  # auth <password>
    writer.write(b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n")
    await writer.drain()

    await reader.readuntil(b"\n\n")  # event json ...
    writer.write(b"Content-Type: command/reply\nReply-Text: +OK\n\n")
    await writer.drain()

    await asyncio.sleep(delay_seconds)

    body = b'{"Event-Name": "CHANNEL_PARK", "Unique-ID": "chan-1"}'
    writer.write(
        f"Content-Type: text/event-json\nContent-Length: {len(body)}\n\n".encode()
        + body
    )
    await writer.drain()
    writer.close()


@pytest.mark.asyncio
async def test_events_does_not_time_out_during_idle_period():
    """Regression test: events() must not apply the constructor's short
    command-reply timeout while waiting for the next event — a real
    FreeSWITCH connection with no in-progress calls can be idle far longer
    than any reasonable command timeout without anything being wrong."""

    async def handler(reader, writer):
        await _run_slow_event_server(reader, writer, delay_seconds=0.3)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]

    async with server:
        # A command-reply timeout far shorter than the idle period below —
        # if events() incorrectly applied this, it would raise well before
        # the event arrives.
        transport = ESLTransport(host, port, "ClueCon", timeout=0.1)
        await transport.connect()
        try:
            await transport.subscribe("CHANNEL_PARK")
            event = await asyncio.wait_for(
                anext(transport.events()), timeout=2.0
            )
            assert event.get("Event-Name") == "CHANNEL_PARK"
            assert event.get("Unique-ID") == "chan-1"
        finally:
            await transport.close()
