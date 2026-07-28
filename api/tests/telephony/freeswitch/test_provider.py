from unittest.mock import AsyncMock, patch

import pytest

from api.enums import TelephonyCallStatus
from api.services.telephony.providers.freeswitch.esl_client import ESLReply
from api.services.telephony.providers.freeswitch.provider import FreeswitchProvider


def _provider(**overrides):
    config = {
        "host": "10.10.10.17",
        "port": 8021,
        "esl_password": "ClueCon",
        "domain": "dograh.local",
        "dial_prefix": "sofia/gateway/my-trunk/",
        "from_numbers": ["1000"],
    }
    config.update(overrides)
    return FreeswitchProvider(config)


class _FakeTransport:
    def __init__(self, api_reply: ESLReply | None = None, bgapi_reply: ESLReply | None = None):
        self.api_reply = api_reply or ESLReply(headers={}, body="+OK")
        self.bgapi_reply = bgapi_reply or ESLReply(
            headers={}, body="+OK Job-UUID: abc-123"
        )
        self.api_calls: list[str] = []
        self.bgapi_calls: list[str] = []
        self.closed = False

    async def connect(self):
        return None

    async def api(self, command: str):
        self.api_calls.append(command)
        return self.api_reply

    async def bgapi(self, command: str):
        self.bgapi_calls.append(command)
        return self.bgapi_reply

    async def close(self):
        self.closed = True


def test_validate_config_true_when_all_required_fields_present():
    provider = _provider()
    assert provider.validate_config() is True


def test_validate_config_false_when_missing_password():
    provider = _provider(esl_password="")
    assert provider.validate_config() is False


@pytest.mark.asyncio
async def test_initiate_call_builds_originate_command_with_channel_vars():
    provider = _provider()
    fake = _FakeTransport()

    with patch(
        "api.services.telephony.providers.freeswitch.provider.ESLTransport",
        return_value=fake,
    ):
        result = await provider.initiate_call(
            to_number="15551230000",
            webhook_url="unused",
            workflow_run_id=42,
            from_number="15550001111",
            workflow_id="7",
        )

    assert len(fake.bgapi_calls) == 1
    command = fake.bgapi_calls[0]
    assert command.startswith("originate {")
    assert "workflow_run_id='42'" in command
    assert "workflow_id='7'" in command
    assert "origination_caller_id_number='15550001111'" in command
    assert "sofia/gateway/my-trunk/15551230000" in command
    assert "&park()" in command
    assert result.status == "initiated"
    assert fake.closed is True


@pytest.mark.asyncio
async def test_initiate_call_raises_on_esl_error():
    provider = _provider()
    fake = _FakeTransport(bgapi_reply=ESLReply(headers={}, body="-ERR NORMAL_TEMPORARY_FAILURE"))

    with patch(
        "api.services.telephony.providers.freeswitch.provider.ESLTransport",
        return_value=fake,
    ):
        with pytest.raises(ValueError):
            await provider.initiate_call(
                to_number="15551230000", webhook_url="unused", workflow_run_id=1
            )


def test_parse_status_callback_maps_channel_answer_to_answered():
    provider = _provider()
    data = {
        "Event-Name": "CHANNEL_ANSWER",
        "Unique-ID": "abc-uuid",
        "Caller-Caller-ID-Number": "15550001111",
        "Caller-Destination-Number": "15551230000",
    }
    result = provider.parse_status_callback(data)
    assert result["status"] == TelephonyCallStatus.ANSWERED
    assert result["call_id"] == "abc-uuid"
    assert result["from_number"] == "15550001111"


def test_parse_status_callback_maps_hangup_complete_to_completed():
    provider = _provider()
    data = {"Event-Name": "CHANNEL_HANGUP_COMPLETE", "Unique-ID": "abc-uuid"}
    result = provider.parse_status_callback(data)
    assert result["status"] == TelephonyCallStatus.COMPLETED


@pytest.mark.asyncio
async def test_transfer_call_originates_destination_with_transfer_id():
    provider = _provider()
    fake = _FakeTransport()

    with patch(
        "api.services.telephony.providers.freeswitch.provider.ESLTransport",
        return_value=fake,
    ):
        result = await provider.transfer_call(
            destination="PJSIP/1234",
            transfer_id="transfer-1",
            conference_name="unused",
            timeout=20,
        )

    assert len(fake.bgapi_calls) == 1
    command = fake.bgapi_calls[0]
    assert "transfer_id='transfer-1'" in command
    assert "sofia/gateway/my-trunk/1234" in command
    assert "&park() 20" in command
    assert result["status"] == "initiated"


def test_supports_transfers_and_get_call_cost():
    provider = _provider()
    assert provider.supports_transfers() is True


@pytest.mark.asyncio
async def test_get_call_cost_returns_unsupported_shape():
    provider = _provider()
    result = await provider.get_call_cost("some-uuid")
    assert result["cost_usd"] == 0.0
    assert "error" in result


def test_can_handle_webhook_always_false():
    assert FreeswitchProvider.can_handle_webhook({}, {}) is False


def test_validate_account_id_always_true():
    assert FreeswitchProvider.validate_account_id({}, "anything") is True
