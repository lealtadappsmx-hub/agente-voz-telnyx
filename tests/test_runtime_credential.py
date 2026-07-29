import base64
import hashlib
import json

import pytest
from cryptography.fernet import Fernet

from runtime_credential import decrypt_gemini_credential_for_voice


SECRET = "shared-secret-only-for-runtime-envelope-tests"


def make_envelope(*, agent_id=1, client_id=2, key="gemini-client-key-test"):
    transport_key = base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest())
    payload = json.dumps(
        {
            "version": 1,
            "agent_id": agent_id,
            "client_id": client_id,
            "gemini_api_key": key,
        },
        separators=(",", ":"),
    ).encode()
    return Fernet(transport_key).encrypt(payload).decode()


def test_decrypts_only_a_credential_bound_to_the_resolved_agent_and_business():
    envelope = make_envelope()

    selected = decrypt_gemini_credential_for_voice(
        envelope=envelope,
        agent_id=1,
        client_id=2,
        shared_secret=SECRET,
    )

    assert selected == "gemini-client-key-test"


@pytest.mark.parametrize(
    "agent_id,client_id,secret",
    [(9, 2, SECRET), (1, 9, SECRET), (1, 2, "different-secret")],
)
def test_rejects_a_credential_outside_its_call_identity(agent_id, client_id, secret):
    with pytest.raises(ValueError, match="runtime credential"):
        decrypt_gemini_credential_for_voice(
            envelope=make_envelope(),
            agent_id=agent_id,
            client_id=client_id,
            shared_secret=secret,
        )
