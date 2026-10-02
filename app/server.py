"""FastAPI app: Vapi webhook receiver, dashboard API, and static dashboard."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, policy, tools, vapi_client

log = logging.getLogger("autopay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Autopay Recovery Voice Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _customer_id_from_call(call: dict[str, Any] | None) -> str | None:
    """Vapi echoes assistantOverrides + metadata back on the call object."""
    if not call:
        return None
    for path in (("metadata", "customer_id"),
                 ("assistantOverrides", "metadata", "customer_id"),
                 ("assistantOverrides", "variableValues", "customer_id")):
        node: Any = call
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node:
            return node
    row = db.get_call(call.get("id", ""))
    return row["customer_id"] if row else None


def _ensure_call_row(call: dict[str, Any] | None, customer_id: str | None, channel: str | None = None) -> str | None:
    if not call or not call.get("id"):
        return None
    call_id = call["id"]
    existing = db.get_call(call_id)
    if not existing:
        db.upsert_call(call_id, customer_id=customer_id, channel=channel or call.get("type", "unknown"),
                       status=call.get("status", "in-progress"), started_at=db.now_iso())
    elif customer_id and not existing.get("customer_id"):
        db.upsert_call(call_id, customer_id=customer_id)
    return call_id


def _pass_fail(value: Any) -> str:
    """Vapi's PassFail rubric returns true/false (sometimes as strings)."""
    if value is None or value == "":
        return ""
    return "PASS" if str(value).strip().lower() in ("true", "pass", "passed", "1") else "FAIL"


def _parse_tool_calls(message: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """Return [(tool_call_id, name, arguments)] across the payload shapes Vapi has used."""
    out: list[tuple[str, str, Any]] = []
    for tc in message.get("toolCallList") or []:
        fn = tc.get("function") or {}
        name = tc.get("name") or fn.get("name")
        args = tc.get("parameters") if "parameters" in tc else fn.get("arguments")
        if tc.get("id") and name:
            out.append((tc["id"], name, args))
    if not out:
        for item in message.get("toolWithToolCallList") or []:
            tc = item.get("toolCall") or {}
            name = item.get("name") or (item.get("function") or {}).get("name")
            if tc.get("id") and name:
                out.append((tc["id"], name, tc.get("parameters") or (tc.get("function") or {}).get("arguments")))
    return out


# ---------------------------------------------------------------------------
# Vapi webhook
# ---------------------------------------------------------------------------

@app.post("/vapi/webhook")
async def vapi_webhook(request: Request) -> JSONResponse:
    if config.VAPI_WEBHOOK_SECRET and request.headers.get("x-vapi-secret") != config.VAPI_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad webhook secret")
    body = await request.json()
    message = body.get("message") or {}
    mtype = message.get("type")
    call = message.get("call") or {}
    customer_id = _customer_id_from_call(call)
    call_id = _ensure_call_row(call, customer_id)

    if mtype == "tool-calls":
        results = []
        for tc_id, name, args in _parse_tool_calls(message):
            result, error = tools.dispatch(name, args, customer_id, call_id)
            if call_id:
                db.add_event(call_id, "tool", name=name, args=args if isinstance(args, dict) else {"raw": args},
                             result=result or None, error=error)
            log.info("tool %s(%s) -> %s", name, args, error or result)
            entry: dict[str, Any] = {"toolCallId": tc_id, "name": name}
            entry["error" if error else "result"] = error or result
            results.append(entry)
        return JSONResponse({"results": results})

    if mtype == "status-update" and call_id:
        status = message.get("status")
        fields: dict[str, Any] = {"status": status}
        if status == "ended":
            fields["ended_at"] = db.now_iso()
            fields["ended_reason"] = message.get("endedReason")
        db.upsert_call(call_id, **fields)
        db.add_event(call_id, "status", status=status)

    elif mtype == "transcript" and call_id:
        if message.get("transcriptType", "final") == "final":
            db.add_event(call_id, "transcript", role=message.get("role"), text=message.get("transcript"))

    elif mtype == "end-of-call-report" and call_id:
        analysis = message.get("analysis") or {}
        artifact = message.get("artifact") or {}
        structured = analysis.get("structuredData")
        db.upsert_call(
            call_id,
            outcome=tools.outcome_from_events((db.get_call(call_id) or {}).get("events", [])),
            status="ended",
            ended_at=db.now_iso(),
            ended_reason=message.get("endedReason"),
            summary=analysis.get("summary"),
            structured=json.dumps(structured) if structured else None,
            success_eval=_pass_fail(analysis.get("successEvaluation")),
            transcript=artifact.get("transcript"),
            duration_seconds=message.get("durationSeconds"),
            cost_usd=message.get("cost"),
        )
        db.add_event(call_id, "note", text="end-of-call report received")
        if customer_id:
            # Every completed call is one contact attempt against the MAX_ATTEMPTS policy.
            cust = db.get_customer(customer_id)
            if cust:
                db.update_state(customer_id, attempts=cust["state"]["attempts"] + 1)

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------

@app.get("/api/config")
def api_config() -> dict[str, Any]:
    return {
        "merchant_name": config.MERCHANT_NAME,
        "agent_name": config.AGENT_NAME,
        "vapi_public_key": config.VAPI_PUBLIC_KEY,
        "assistant_id": config.VAPI_ASSISTANT_ID,
        "web_calls_ready": bool(config.VAPI_PUBLIC_KEY and config.VAPI_ASSISTANT_ID),
        "phone_calls_ready": bool(config.VAPI_PRIVATE_KEY and config.VAPI_ASSISTANT_ID and config.VAPI_PHONE_NUMBER_ID),
        "calling_window": f"{config.CALL_WINDOW_START}-{config.CALL_WINDOW_END} {config.CALL_TIMEZONE}",
        "max_attempts": config.MAX_ATTEMPTS,
    }


@app.get("/api/customers")
def api_customers() -> list[dict[str, Any]]:
    out = []
    for c in db.list_customers():
        ok, why = policy.eligibility(c)
        c["eligible"] = ok
        c["eligibility_reason"] = why
        c.pop("verify_last4", None)   # never ship the secret to the browser
        out.append(c)
    return out


@app.get("/api/customers/{customer_id}/secret")
def api_customer_secret(customer_id: str) -> dict[str, Any]:
    """Demo helper: the person role-playing the customer needs the 4 digits."""
    cust = db.get_customer(customer_id)
    if not cust:
        raise HTTPException(404)
    actions = []
    if cust["state"].get("mandate_status") == "paused":
        actions.append({"action": "resume_mandate", "label": "Resume mandate in UPI app"})
    if not cust["state"].get("funds_available"):
        actions.append({"action": "add_funds", "label": "Add funds to account"})
    return {"verify_last4": cust["verify_last4"], "persona": cust["persona"], "actions": actions}


@app.post("/api/customers/{customer_id}/customer-action")
async def api_customer_action(customer_id: str, request: Request) -> dict[str, Any]:
    """Demo stand-in for what the real customer does on their own phone or bank app.

    This is the only way gateway ground truth changes; nothing said on a call can do it.
    """
    body = await request.json()
    try:
        state = tools.customer_action(customer_id, body.get("action", ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    open_call = db.latest_open_call(customer_id)
    if open_call:      # keep it on the call's timeline: it explains why a later tool result changed
        db.add_event(open_call, "action", text=f"customer, on their own phone: {body['action'].replace('_', ' ')}")
    return {"ok": True, "mandate_status": state["mandate_status"], "funds_available": state["funds_available"]}


@app.get("/api/calls")
def api_calls() -> list[dict[str, Any]]:
    return db.list_calls()


@app.get("/api/calls/{call_id}")
def api_call(call_id: str) -> dict[str, Any]:
    call = db.get_call(call_id)
    if not call:
        raise HTTPException(404)
    return call


@app.post("/api/calls/register")
async def api_register_call(request: Request) -> dict[str, Any]:
    body = await request.json()
    call_id, customer_id = body.get("call_id"), body.get("customer_id")
    if not call_id or not customer_id:
        raise HTTPException(400, "call_id and customer_id required")
    db.upsert_call(call_id, customer_id=customer_id, channel=body.get("channel", "web"),
                   status="in-progress", started_at=db.now_iso())
    return {"ok": True}


@app.get("/api/web-call/{customer_id}")
def api_web_call(customer_id: str, force: bool = False) -> dict[str, Any]:
    cust = db.get_customer(customer_id)
    if not cust:
        raise HTTPException(404)
    # force = dashboard role-play: may replay scenarios outside the window, but never a do-not-call customer
    ok, why = policy.eligibility(cust, demo=force)
    if not ok:
        raise HTTPException(409, f"not eligible: {why}")
    if not config.VAPI_ASSISTANT_ID:
        raise HTTPException(503, "VAPI_ASSISTANT_ID not configured; run scripts/setup_assistant.py")
    return {"assistant_id": config.VAPI_ASSISTANT_ID, "assistant_overrides": vapi_client.assistant_overrides(cust)}


@app.post("/api/calls/phone/{customer_id}")
async def api_phone_call(customer_id: str, request: Request) -> dict[str, Any]:
    cust = db.get_customer(customer_id)
    if not cust:
        raise HTTPException(404)
    body = await request.json() if await request.body() else {}
    to_number = body.get("to_number")   # a number you control; never the fictional one on file
    if not to_number:
        raise HTTPException(400, "to_number is required: dial only a number you own or have permission to call")
    ok, why = policy.eligibility(cust, ignore_window=bool(body.get("force")))
    if not ok:
        raise HTTPException(409, f"not eligible: {why}")
    try:
        res = vapi_client.create_phone_call(cust, to_number)
    except vapi_client.VapiError as e:
        raise HTTPException(502, str(e)) from e
    db.upsert_call(res["id"], customer_id=customer_id, channel="phone", status=res.get("status", "queued"),
                   started_at=db.now_iso())
    return {"call_id": res["id"], "status": res.get("status")}


@app.post("/api/campaign/plan")
def api_campaign_plan() -> dict[str, Any]:
    """Which customers the campaign would dial right now, and why the rest are skipped."""
    plan = []
    for c in db.list_customers():
        ok, why = policy.eligibility(c)
        plan.append({"customer_id": c["id"], "name": c["name"], "dial": ok, "reason": why})
    return {"plan": plan, "window": f"{config.CALL_WINDOW_START}-{config.CALL_WINDOW_END} {config.CALL_TIMEZONE}"}


@app.post("/api/customers/{customer_id}/simulate-link-payment")
def api_simulate_link_payment(customer_id: str) -> dict[str, Any]:
    """Demo helper standing in for the payment-link webhook from the gateway."""
    cust = db.get_customer(customer_id)
    if not cust:
        raise HTTPException(404)
    if not cust["state"].get("payment_link"):
        raise HTTPException(409, "no payment link has been sent to this customer")
    st = db.update_state(customer_id, link_paid=True, recovery_status="recovered",
                         recovered_amount_inr=cust["amount_inr"], last_outcome="paid_via_link")
    return {"ok": True, "state": st}


@app.post("/api/reset")
def api_reset() -> dict[str, Any]:
    db.reset_all()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
