"""FreeSWITCH (ESL) telephony provider package."""

from typing import Any, Dict

from fastapi import HTTPException
from loguru import logger

from api.services.telephony.registry import (
    ProviderSpec,
    ProviderUIField,
    ProviderUIMetadata,
    register,
)

from .config import FreeswitchConfigurationRequest, FreeswitchConfigurationResponse
from .esl_client import ESLAuthError, ESLTransport
from .provider import FreeswitchProvider
from .transport import create_transport


def _config_loader(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "provider": "freeswitch",
        "host": value.get("host"),
        "port": value.get("port", 8021),
        "esl_password": value.get("esl_password"),
        "domain": value.get("domain"),
        "dial_prefix": value.get("dial_prefix"),
        "audio_bridge_module": value.get("audio_bridge_module", "mod_audio_stream"),
        "from_numbers": value.get("from_numbers", []),
    }


async def _test_esl_connection(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """Attempt a live ESL auth handshake before saving.

    This is the actual "test connection" check for this provider —
    `validate_config()` on the provider class can't do it since its ABC
    signature is synchronous, but this hook is async and I/O-permitted
    (per registry.py's ProviderSpec.preprocess_credentials_on_save docs).
    Raising HTTPException here aborts the save with a clear error instead of
    silently persisting unreachable credentials. Note this only proves ESL
    reachability/auth — it cannot validate `dial_prefix`, which is entirely
    operator-defined and only surfaces as an error on the first real call.
    """
    host = credentials.get("host")
    port = credentials.get("port", 8021)
    password = credentials.get("esl_password")
    if not host or not password:
        return credentials

    transport = ESLTransport(host, int(port), password, timeout=5.0)
    try:
        await transport.connect()
    except ESLAuthError as e:
        raise HTTPException(
            status_code=400,
            detail=f"FreeSWITCH ESL authentication failed: {e}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not reach FreeSWITCH ESL at {host}:{port}: {e}",
        )
    else:
        logger.info(f"[FreeSWITCH] ESL connection test to {host}:{port} succeeded")
    finally:
        await transport.close()

    return credentials


_UI_METADATA = ProviderUIMetadata(
    display_name="FreeSWITCH",
    docs_url="https://docs.dograh.com/integrations/telephony/freeswitch",
    fields=[
        ProviderUIField(
            name="host",
            label="ESL Host",
            type="text",
            description="FreeSWITCH box's ESL-reachable hostname or IP",
        ),
        ProviderUIField(
            name="port",
            label="ESL Port",
            type="number",
            required=False,
            placeholder="8021",
            description="mod_event_socket port (default 8021)",
        ),
        ProviderUIField(
            name="esl_password",
            label="ESL Password",
            type="password",
            sensitive=True,
            description="mod_event_socket password",
        ),
        ProviderUIField(
            name="domain",
            label="Domain",
            type="text",
            description="FreeSWITCH SIP domain / FusionPBX domain",
        ),
        ProviderUIField(
            name="dial_prefix",
            label="Dial Prefix",
            type="text",
            placeholder="sofia/gateway/my-trunk/",
            description=(
                "Dial-string prefix for outbound calls, e.g. "
                "sofia/gateway/<name>/ or sofia/internal/. Operator-specific — "
                "cannot be validated until the first call attempt."
            ),
        ),
        ProviderUIField(
            name="from_numbers",
            label="From Extensions",
            type="string-array",
            required=False,
            description="Extensions/DIDs available for outbound calls",
        ),
    ],
)


SPEC = ProviderSpec(
    name="freeswitch",
    provider_cls=FreeswitchProvider,
    config_loader=_config_loader,
    transport_factory=create_transport,
    transport_sample_rate=8000,
    config_request_cls=FreeswitchConfigurationRequest,
    config_response_cls=FreeswitchConfigurationResponse,
    ui_metadata=_UI_METADATA,
    # No HTTP webhook / account-id concept — mirrors ari. See DESIGN.md for
    # the multi-FreeSWITCH-box-per-org caveat this implies.
    account_id_credential_field="",
    preprocess_credentials_on_save=_test_esl_connection,
)


register(SPEC)


__all__ = [
    "SPEC",
    "FreeswitchConfigurationRequest",
    "FreeswitchConfigurationResponse",
    "FreeswitchProvider",
    "create_transport",
]
