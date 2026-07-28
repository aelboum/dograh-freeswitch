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
