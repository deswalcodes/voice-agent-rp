"""Create (or update) the Vapi assistant from app/prompt.py + app/tools.py and store its id in .env.

    python -m scripts.setup_assistant            # create new, or update if VAPI_ASSISTANT_ID is set
    python -m scripts.setup_assistant --print    # just print the payload that would be sent
"""
from __future__ import annotations

import json
import re
import sys

from app import config, vapi_client


def write_env(key: str, value: str) -> None:
    env_path = config.ROOT / ".env"
    text = env_path.read_text() if env_path.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    env_path.write_text(text)


def main() -> int:
    if "--print" in sys.argv:
        if not config.PUBLIC_BASE_URL:
            config.PUBLIC_BASE_URL = "https://<your-ngrok-host>"
        print(json.dumps(vapi_client.build_assistant_payload(), indent=2))
        return 0
    try:
        if config.VAPI_ASSISTANT_ID:
            a = vapi_client.update_assistant(config.VAPI_ASSISTANT_ID)
            print(f"Updated assistant {a['id']} ({a.get('name')})")
        else:
            a = vapi_client.create_assistant()
            write_env("VAPI_ASSISTANT_ID", a["id"])
            print(f"Created assistant {a['id']} ({a.get('name')}) and saved VAPI_ASSISTANT_ID to .env")
    except vapi_client.VapiError as e:
        print(f"Vapi error: {e}", file=sys.stderr)
        print("Common causes: bad VAPI_PRIVATE_KEY, PUBLIC_BASE_URL not https, or a voice/model name Vapi does not know "
              "(change VAPI_VOICE_ID / VAPI_MODEL in .env).", file=sys.stderr)
        return 1
    print(f"Webhook: {vapi_client.webhook_url()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
