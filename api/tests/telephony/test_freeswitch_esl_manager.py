from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.telephony.providers.freeswitch import esl_manager
from api.services.telephony.providers.freeswitch.esl_client import ESLEvent
from api.services.telephony.providers.freeswitch.esl_manager import ESLConnection


@pytest.fixture
def fake_call_concurrency(monkeypatch):
    fake = SimpleNamespace(unregister_active_call=AsyncMock(return_value=True))
    monkeypatch.setattr(esl_manager, "call_concurrency", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_db_client(monkeypatch):
    monkeypatch.setattr(
        esl_manager.db_client, "update_workflow_run", AsyncMock(return_value=None)
    )


class _RecordingConnection(ESLConnection):
    def __init__(self):
        super().__init__(
            organization_id=1,
            telephony_configuration_id=10,
            host="10.10.10.17",
            port=8021,
            esl_password="ClueCon",
            dial_prefix="sofia/gateway/my-trunk/",
        )
        self._runs: dict[str, str] = {}
        self.attached_media: list[tuple] = []
        self.answered: list[str] = []
        self.hungup: list[str] = []
        self.destination_answered: list[tuple] = []

    async def _set_channel_run(self, channel_id: str, workflow_run_id: str):
        self._runs[channel_id] = workflow_run_id

    async def _get_channel_run(self, channel_id: str):
        return self._runs.get(channel_id)

    async def _delete_channel_run(self, channel_id: str):
        self._runs.pop(channel_id, None)

    async def _attach_media(self, channel_id, workflow_run_id, workflow_id):
        self.attached_media.append((channel_id, workflow_run_id, workflow_id))

    async def _answer(self, channel_id: str):
        self.answered.append(channel_id)

    async def _hangup(self, channel_id: str):
        self.hungup.append(channel_id)

    async def _handle_destination_answered(self, transfer_id: str, channel_id: str):
        self.destination_answered.append((transfer_id, channel_id))


def _event(**data) -> ESLEvent:
    return ESLEvent(headers={}, body="", data=data)


@pytest.mark.asyncio
async def test_channel_park_for_outbound_leg_attaches_media(fake_call_concurrency):
    conn = _RecordingConnection()
    event = _event(variable_workflow_run_id="42", variable_workflow_id="7")

    await conn._handle_channel_park("chan-1", event)

    assert conn.attached_media == [("chan-1", 42, 7)]
    assert conn._runs["chan-1"] == "42"


@pytest.mark.asyncio
async def test_channel_park_for_transfer_destination_does_not_attach_media(
    fake_call_concurrency,
):
    conn = _RecordingConnection()
    event = _event(variable_transfer_id="transfer-1")

    await conn._handle_channel_park("chan-dest", event)

    assert conn.attached_media == []
    assert "chan-dest" not in conn._runs


@pytest.mark.asyncio
async def test_channel_answer_for_transfer_destination_correlates(fake_call_concurrency):
    conn = _RecordingConnection()
    event = _event(variable_transfer_id="transfer-1")

    await conn._handle_channel_answer("chan-dest", event)

    assert conn.destination_answered == [("transfer-1", "chan-dest")]


@pytest.mark.asyncio
async def test_channel_answer_without_transfer_id_is_ignored(fake_call_concurrency):
    conn = _RecordingConnection()
    event = _event()

    await conn._handle_channel_answer("chan-1", event)

    assert conn.destination_answered == []


@pytest.mark.asyncio
async def test_channel_hangup_unregisters_active_call_and_clears_mapping(
    fake_call_concurrency,
):
    conn = _RecordingConnection()
    conn._runs["chan-1"] = "42"
    event = _event(Event_Name="CHANNEL_HANGUP_COMPLETE")

    await conn._handle_channel_hangup("chan-1", event)

    fake_call_concurrency.unregister_active_call.assert_awaited_once_with(42)
    assert "chan-1" not in conn._runs


@pytest.mark.asyncio
async def test_channel_hangup_for_untracked_channel_is_a_noop(fake_call_concurrency):
    conn = _RecordingConnection()
    event = _event(Event_Name="CHANNEL_HANGUP_COMPLETE")

    await conn._handle_channel_hangup("unknown-chan", event)

    fake_call_concurrency.unregister_active_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_event_dispatches_channel_park(fake_call_concurrency):
    conn = _RecordingConnection()
    event = _event(
        **{
            "Event-Name": "CHANNEL_PARK",
            "Unique-ID": "chan-1",
            "variable_workflow_run_id": "42",
            "variable_workflow_id": "7",
        }
    )

    await conn._handle_event(event)

    assert conn.attached_media == [("chan-1", 42, 7)]
