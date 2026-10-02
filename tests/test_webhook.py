import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.server import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def call(customer_id="cust_004", call_id="call_1"):
    return {"id": call_id, "type": "webCall", "assistantOverrides": {"variableValues": {"customer_id": customer_id}}}


def tool(client, name, params, customer_id="cust_004", call_id="call_1", tc_id="tc"):
    r = client.post("/vapi/webhook", json={"message": {"type": "tool-calls", "call": call(customer_id, call_id),
        "toolCallList": [{"id": tc_id, "name": name, "parameters": params}]}})
    assert r.status_code == 200
    return r.json()["results"][0]


def test_tool_calls_new_shape(client):
    res = tool(client, "verify_identity", {"customer_id": "cust_004", "spoken_digits": "5 5 9 0"}, tc_id="tc1")
    assert res["toolCallId"] == "tc1" and res["name"] == "verify_identity" and res["result"].startswith("VERIFIED")


def test_tool_calls_openai_shape_and_customer_fallback(client):
    tool(client, "verify_identity", {"spoken_digits": "5590"})          # no customer_id: falls back to the call's variableValues
    r = client.post("/vapi/webhook", json={"message": {"type": "tool-calls", "call": call(),
        "toolCallList": [{"id": "tc2", "type": "function",
                          "function": {"name": "retry_payment", "arguments": "{\"customer_consented\": true}"}}]}})
    assert r.json()["results"][0]["result"].startswith("SUCCESS")
    assert db.get_customer("cust_004")["state"]["recovery_status"] == "recovered"


def test_verification_does_not_leak_across_calls(client):
    tool(client, "verify_identity", {"spoken_digits": "5590"}, call_id="call_1")
    res = tool(client, "retry_payment", {"customer_consented": True}, call_id="call_2")
    assert "error" in res and "not verified on this call" in res["error"]


def test_tool_error_is_returned_not_raised(client):
    res = tool(client, "retry_payment", {"customer_id": "cust_004", "customer_consented": True})
    assert "error" in res and "not verified" in res["error"]


def test_live_call_regression_claimed_resume_is_checked_with_the_bank(client):
    c = dict(customer_id="cust_003", call_id="call_r")
    assert tool(client, "verify_identity", {"spoken_digits": "एक 0 3 2"}, **c)["result"].startswith("VERIFIED")
    assert tool(client, "check_mandate_status", {}, **c)["result"].startswith("PAUSED")
    assert tool(client, "retry_payment", {"customer_consented": True, "mandate_resumed": True}, **c)["result"].startswith("FAILED")

    r = client.post("/api/customers/cust_003/customer-action", json={"action": "resume_mandate"})
    assert r.status_code == 200 and r.json()["mandate_status"] == "active"
    assert tool(client, "check_mandate_status", {}, **c)["result"].startswith("ACTIVE")
    assert tool(client, "retry_payment", {"customer_consented": True}, **c)["result"].startswith("SUCCESS")
    assert client.post("/api/customers/cust_003/customer-action", json={"action": "resume_mandate"}).status_code == 400


def test_secret_endpoint_lists_customer_actions(client):
    assert [a["action"] for a in client.get("/api/customers/cust_003/secret").json()["actions"]] == ["resume_mandate"]
    assert [a["action"] for a in client.get("/api/customers/cust_001/secret").json()["actions"]] == ["add_funds"]
    assert client.get("/api/customers/cust_004/secret").json()["actions"] == []


def test_webhook_secret(client, monkeypatch):
    monkeypatch.setattr(config, "VAPI_WEBHOOK_SECRET", "s3cret")
    body = {"message": {"type": "status-update", "status": "in-progress", "call": call()}}
    assert client.post("/vapi/webhook", json=body).status_code == 401
    assert client.post("/vapi/webhook", json=body, headers={"x-vapi-secret": "s3cret"}).status_code == 200


@pytest.mark.parametrize("raw,expected", [(True, "PASS"), ("true", "PASS"), (False, "FAIL"), ("false", "FAIL"), (None, "")])
def test_end_of_call_report_persists_and_counts_attempt(client, raw, expected):
    client.post("/vapi/webhook", json={"message": {"type": "transcript", "call": call(), "role": "user",
                                                    "transcriptType": "final", "transcript": "hello"}})
    r = client.post("/vapi/webhook", json={"message": {"type": "end-of-call-report", "call": call(), "endedReason": "customer-ended-call",
        "durationSeconds": 42.5, "artifact": {"transcript": "AI: hi\nUser: hello"},
        "analysis": {"summary": "No resolution.", "structuredData": {"outcome": "no_resolution"}, "successEvaluation": raw}}})
    assert r.status_code == 200
    k = client.get("/api/calls/call_1").json()
    assert k["summary"] == "No resolution." and k["structured"]["outcome"] == "no_resolution" and k["success_eval"] == expected
    assert [e for e in k["events"] if e["kind"] == "transcript"][0]["text"] == "hello"
    assert db.get_customer("cust_004")["state"]["attempts"] == 2      # every completed call is one contact attempt


def test_dashboard_endpoints(client):
    customers = client.get("/api/customers").json()
    assert len(customers) == 10 and all("verify_last4" not in c for c in customers)
    assert all(c["plan"].startswith("StreamBox") for c in customers)   # one merchant, no brand mismatch on the call
    dnc = next(c for c in customers if c["id"] == "cust_006")
    assert not dnc["eligible"] and "do-not-call" in dnc["eligibility_reason"]
    plan = client.post("/api/campaign/plan").json()["plan"]
    assert any(p["customer_id"] == "cust_006" and not p["dial"] for p in plan)
    assert client.get("/api/customers/cust_001/secret").json()["verify_last4"] == "4421"


def test_phone_call_requires_number_you_control(client):
    r = client.post("/api/calls/phone/cust_001", json={})
    assert r.status_code == 400 and "number you own" in r.json()["detail"]


def test_simulate_link_payment(client):
    assert client.post("/api/customers/cust_002/simulate-link-payment").status_code == 409
    c = dict(customer_id="cust_002", call_id="call_p")
    tool(client, "verify_identity", {"spoken_digits": "7788"}, **c)
    tool(client, "send_payment_link", {"purpose": "new_mandate"}, **c)
    r = client.post("/api/customers/cust_002/simulate-link-payment")
    assert r.status_code == 200 and r.json()["state"]["recovery_status"] == "recovered"


def test_web_call_refuses_do_not_call_customer_even_with_force(client, monkeypatch):
    """Regression: the dashboard's demo override let a do-not-call customer be dialled."""
    monkeypatch.setattr(config, "VAPI_ASSISTANT_ID", "asst_test")
    r = client.get("/api/web-call/cust_006?force=true")
    assert r.status_code == 409 and "do-not-call" in r.json()["detail"]
    assert client.get("/api/web-call/cust_001?force=true").status_code == 200


def test_call_outcome_is_derived_from_tools_not_from_the_llm_analysis(client):
    c = dict(customer_id="cust_008", call_id="call_o")
    tool(client, "verify_identity", {"spoken_digits": "9174"}, **c)
    from datetime import date, timedelta
    tool(client, "schedule_retry", {"retry_date": (date.today() + timedelta(days=1)).isoformat()}, **c)
    client.post("/vapi/webhook", json={"message": {"type": "end-of-call-report", "call": call("cust_008", "call_o"),
        "analysis": {"summary": "A human must schedule the retry.", "structuredData": {"outcome": "no_resolution"},
                     "successEvaluation": "false"}}})
    k = client.get("/api/calls/call_o").json()
    assert k["outcome"] == "promise_to_pay"            # what actually happened
    assert k["structured"]["outcome"] == "no_resolution"   # what the transcript-only analysis guessed


def test_customer_action_is_logged_on_the_open_call_timeline(client):
    client.post("/api/calls/register", json={"call_id": "call_t", "customer_id": "cust_003", "channel": "web"})
    client.post("/api/customers/cust_003/customer-action", json={"action": "resume_mandate"})
    events = client.get("/api/calls/call_t").json()["events"]
    assert [e["text"] for e in events if e["kind"] == "action"] == ["customer, on their own phone: resume mandate"]
