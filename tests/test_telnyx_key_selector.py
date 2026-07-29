import logging
from pathlib import Path

from panel_config_client import PanelAgentObservation
from telnyx_key_selector import select_telnyx_api_key
from tests.test_runtime_credential import SECRET, make_telnyx_envelope


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TELNYX_KEY = "telnyx-client-key-test"


def observation(envelope: str | None) -> PanelAgentObservation:
    return PanelAgentObservation(
        agent_id=1,
        client_id=2,
        agent_name="Luisa",
        system_prompt="Prompt dinámico completo para el negocio resuelto.",
        elapsed_ms=25.0,
        telnyx_credential_envelope=envelope,
    )


def test_selects_business_telnyx_key_without_logging_it(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    selected = select_telnyx_api_key(
        observation(make_telnyx_envelope(key=TELNYX_KEY)),
        shared_secret=SECRET,
    )

    assert selected == TELNYX_KEY
    assert "source=negocio agent_id=1 client_id=2" in caplog.text
    assert TELNYX_KEY not in caplog.text


def test_bridge_has_no_global_telnyx_key_fallback():
    source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'os.getenv("TELNYX_API_KEY"' not in source
    assert "\nTELNYX_API_KEY=" not in f"\n{env_example}"
