"""Thin client for the Vapi REST API plus the assistant definition we publish there."""
from __future__ import annotations

from typing import Any

import httpx

from . import config, prompt, tools


class VapiError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    if not config.VAPI_PRIVATE_KEY:
        raise VapiError("VAPI_PRIVATE_KEY is not set (see .env.example)")
    return {"Authorization": f"Bearer {config.VAPI_PRIVATE_KEY}", "Content-Type": "application/json"}


def _request(method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(base_url=config.VAPI_API_BASE, timeout=30) as client:
        r = client.request(method, path, headers=_headers(), json=json)
    if r.status_code >= 400:
        raise VapiError(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
    return r.json() if r.content else {}


# ---------------------------------------------------------------------------
# Assistant definition
# ---------------------------------------------------------------------------

def webhook_url() -> str:
    if not config.PUBLIC_BASE_URL:
        raise VapiError("PUBLIC_BASE_URL is not set; run ngrok and put the https URL in .env")
    return f"{config.PUBLIC_BASE_URL}/vapi/webhook"


def transcriber_for(language: str) -> dict[str, Any]:
    """Deepgram nova-3: 'en' for English, 'multi' for Hindi/English code-switching."""
    lang = "multi" if language in ("hinglish", "hi") else "en"
    return {"provider": "deepgram", "model": "nova-3", "language": lang}


def build_assistant_payload() -> dict[str, Any]:
    server: dict[str, Any] = {"url": webhook_url(), "timeoutSeconds": 20}
    if config.VAPI_WEBHOOK_SECRET:
        server["secret"] = config.VAPI_WEBHOOK_SECRET
    return {
        "name": f"{config.MERCHANT_NAME} Autopay Recovery",
        "firstMessage": prompt.FIRST_MESSAGE,
        "firstMessageMode": "assistant-speaks-first",
        "model": {
            "provider": config.VAPI_MODEL_PROVIDER,
            "model": config.VAPI_MODEL,
            "temperature": 0.3,
            "maxTokens": 250,
            "messages": [{"role": "system", "content": prompt.SYSTEM_PROMPT}],
            "tools": tools.vapi_tools(webhook_url(), config.VAPI_WEBHOOK_SECRET or None),
        },
        "voice": {"provider": config.VAPI_VOICE_PROVIDER, "voiceId": config.VAPI_VOICE_ID},
        "transcriber": transcriber_for("en"),
        "server": server,
        "serverMessages": ["tool-calls", "status-update", "transcript", "end-of-call-report"],
        "analysisPlan": prompt.ANALYSIS_PLAN,
        "maxDurationSeconds": 240,
        "silenceTimeoutSeconds": 20,
        # The agent has no hang-up tool. The call ends when it says goodbye, which forces it to
        # speak the outcome first instead of disconnecting silently after a tool call.
        "endCallPhrases": ["goodbye", "good bye"],
        "endCallMessage": "",          # explicit empty: no canned sign-off after the agent's own goodbye
        "backgroundSound": "off",
        "metadata": {"app": "autopay-recovery-agent"},
    }


def create_assistant() -> dict[str, Any]:
    return _request("POST", "/assistant", build_assistant_payload())


def update_assistant(assistant_id: str) -> dict[str, Any]:
    return _request("PATCH", f"/assistant/{assistant_id}", build_assistant_payload())


def get_assistant(assistant_id: str) -> dict[str, Any]:
    return _request("GET", f"/assistant/{assistant_id}")


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

def assistant_overrides(customer: dict[str, Any]) -> dict[str, Any]:
    return {
        "variableValues": prompt.variable_values(customer),
        "transcriber": transcriber_for(customer.get("preferred_language", "en")),
        "metadata": {"customer_id": customer["id"]},
    }


def create_phone_call(customer: dict[str, Any], to_number: str) -> dict[str, Any]:
    if not config.VAPI_ASSISTANT_ID:
        raise VapiError("VAPI_ASSISTANT_ID is not set; run scripts/setup_assistant.py first")
    if not config.VAPI_PHONE_NUMBER_ID:
        raise VapiError("VAPI_PHONE_NUMBER_ID is not set; import a Twilio/Telnyx number in Vapi to place outbound calls")
    body = {
        "assistantId": config.VAPI_ASSISTANT_ID,
        "phoneNumberId": config.VAPI_PHONE_NUMBER_ID,
        "customer": {"number": to_number, "name": customer["name"]},
        "assistantOverrides": assistant_overrides(customer),
        "metadata": {"customer_id": customer["id"]},
    }
    return _request("POST", "/call/phone", body)
