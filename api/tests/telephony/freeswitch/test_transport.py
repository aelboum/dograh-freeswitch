import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)

from api.services.telephony.providers.freeswitch.esl_client import ESLReply
from api.services.telephony.providers.freeswitch.serializers import (
    FreeswitchFrameSerializer,
)


class _FakeESLTransport:
    """Mirrors test_provider.py's _FakeTransport, scoped to api()/close() —
    _break_playback never calls bgapi()."""

    def __init__(self, api_reply: ESLReply | None = None, raise_on_connect: bool = False):
        self.api_reply = api_reply or ESLReply(headers={}, body="+OK")
        self.api_calls: list[str] = []
        self.closed = False
        self._raise_on_connect = raise_on_connect

    async def connect(self):
        if self._raise_on_connect:
            raise ConnectionError("boom")

    async def api(self, command: str):
        self.api_calls.append(command)
        return self.api_reply

    async def close(self):
        self.closed = True


def _serializer(**overrides):
    hangup_strategy = overrides.pop("hangup_strategy", None)
    transfer_strategy = overrides.pop("transfer_strategy", None)
    return FreeswitchFrameSerializer(
        channel_id="chan-uuid",
        host="10.10.10.17",
        port=8021,
        esl_password="ClueCon",
        hangup_strategy=hangup_strategy,
        transfer_strategy=transfer_strategy,
    )


@pytest.mark.asyncio
async def test_deserialize_binary_audio_produces_input_frame():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    # 8000Hz L16 mono silence, matching freeswitch_sample_rate == pipeline rate
    # so the resampler is a passthrough (no ratio conversion needed).
    raw_pcm = b"\x00\x00" * 160
    frame = await serializer.deserialize(raw_pcm)

    assert isinstance(frame, InputAudioRawFrame)
    assert frame.num_channels == 1
    assert frame.sample_rate == 8000


@pytest.mark.asyncio
async def test_deserialize_verbatim_text_metadata_is_ignored():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    frame = await serializer.deserialize("dograh-run-42")
    assert frame is None


@pytest.mark.asyncio
async def test_serialize_audio_frame_wraps_json_base64_stream_audio():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    # A single small frame is held in the playback/priming buffer and yields
    # nothing (see test_serialize_audio_frame_below_threshold_buffers_silently
    # below) — send enough audio to clear both the default 250ms
    # playback_buffer_ms and the first-utterance 350ms priming_lead_ms
    # thresholds (600ms => 9600 bytes at 8000Hz/16-bit) in one frame so this
    # test can focus purely on the streamAudio wrapping shape.
    pcm = (b"\x01\x02" * 160) * 100  # 32000 bytes, well over the 9600 byte floor
    frame = OutputAudioRawFrame(audio=pcm, sample_rate=8000, num_channels=1)
    serialized = await serializer.serialize(frame)

    assert isinstance(serialized, str)
    message = json.loads(serialized)
    assert message["type"] == "streamAudio"
    assert message["data"]["audioDataType"] == "raw"
    assert message["data"]["sampleRate"] == 8000
    decoded = base64.b64decode(message["data"]["audioData"])
    assert len(decoded) > 0


@pytest.mark.asyncio
async def test_serialize_audio_frame_below_threshold_buffers_silently():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    pcm = b"\x01\x02" * 160  # 320 bytes, far under the 9600 byte flush floor
    frame = OutputAudioRawFrame(audio=pcm, sample_rate=8000, num_channels=1)
    serialized = await serializer.serialize(frame)

    assert serialized is None
    assert bytes(serializer._playback_buffer) == pcm


@pytest.mark.asyncio
async def test_serialize_end_frame_calls_hangup_strategy():
    hangup_strategy = AsyncMock()
    hangup_strategy.execute_hangup = AsyncMock(return_value=True)
    serializer = _serializer(hangup_strategy=hangup_strategy)
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    result = await serializer.serialize(EndFrame())

    assert result is None
    # Hangup now runs via a fire-and-forget asyncio.create_task (deferred so
    # any buffered trailing audio gets a chance to play out first); give it a
    # turn on the loop before asserting on it.
    await asyncio.sleep(0)
    hangup_strategy.execute_hangup.assert_awaited_once()
    context = hangup_strategy.execute_hangup.call_args.args[0]
    assert context["channel_id"] == "chan-uuid"
    assert context["host"] == "10.10.10.17"


@pytest.mark.asyncio
async def test_serialize_interruption_frame_breaks_playback_and_clears_buffer():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    # Prime the buffer as if a chunk were mid-accumulation, so we can assert
    # the interruption drops it instead of leaking it into the next utterance.
    serializer._playback_buffer.extend(b"\x00\x01" * 10)
    serializer._priming = False

    fake_transport = _FakeESLTransport()
    with patch(
        "api.services.telephony.providers.freeswitch.serializers.ESLTransport",
        return_value=fake_transport,
    ):
        result = await serializer.serialize(InterruptionFrame())
        assert result is None
        # _break_playback runs as a fire-and-forget asyncio.create_task; give
        # it a turn on the loop before asserting on it.
        await asyncio.sleep(0)

    assert fake_transport.api_calls == ["uuid_break chan-uuid all"]
    assert fake_transport.closed is True
    assert bytes(serializer._playback_buffer) == b""
    assert serializer._priming is True


@pytest.mark.asyncio
async def test_serialize_interruption_frame_logs_but_does_not_raise_on_bad_reply():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    fake_transport = _FakeESLTransport(
        api_reply=ESLReply(headers={}, body="-ERR No Such Channel")
    )
    with patch(
        "api.services.telephony.providers.freeswitch.serializers.ESLTransport",
        return_value=fake_transport,
    ):
        result = await serializer.serialize(InterruptionFrame())
        assert result is None
        await asyncio.sleep(0)

    assert fake_transport.api_calls == ["uuid_break chan-uuid all"]


@pytest.mark.asyncio
async def test_serialize_interruption_frame_swallows_transport_connect_error():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    fake_transport = _FakeESLTransport(raise_on_connect=True)
    with patch(
        "api.services.telephony.providers.freeswitch.serializers.ESLTransport",
        return_value=fake_transport,
    ):
        result = await serializer.serialize(InterruptionFrame())
        assert result is None
        # Must not raise / propagate out of the fire-and-forget task.
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_create_transport_raises_when_credentials_incomplete():
    from api.services.telephony.providers.freeswitch.transport import create_transport

    with patch(
        "api.services.telephony.providers.freeswitch.transport.load_credentials_for_transport",
        new=AsyncMock(return_value={"host": "", "esl_password": ""}),
    ):
        from unittest.mock import MagicMock

        from api.services.pipecat.audio_config import AudioConfig

        with pytest.raises(ValueError):
            await create_transport(
                MagicMock(),
                workflow_run_id=1,
                audio_config=AudioConfig(
                    transport_in_sample_rate=8000,
                    transport_out_sample_rate=8000,
                    pipeline_sample_rate=16000,
                ),
                organization_id=2,
                channel_id="chan-uuid",
            )
