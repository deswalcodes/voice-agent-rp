from datetime import date, timedelta

import pytest

from app import db, policy, tools

CALL = "call-A"


def run(name, call_id=CALL, **args):
    return tools.dispatch(name, args, None, call_id)


def verify(cid, digits, call_id=CALL):
    return run("verify_identity", call_id, customer_id=cid, spoken_digits=digits)[0]


# ------------------------------------------------------------------ spoken digits

@pytest.mark.parametrize("spoken,expected", [
    ("4421", "4421"),
    ("4 4 2 1", "4421"),
    ("44-21", "4421"),
    ("double 4 2 1", "4421"),                 # the live-call failure
    ("double four two one", "4421"),
    ("It's double four, two, one.", "4421"),
    ("एक 0 3 2", "1032"),                      # the other live-call failure
    ("एक शून्य तीन दो", "1032"),
    ("१०३२", "1032"),
    ("ek shunya teen do", "1032"),
    ("triple 7 8", "7778"),
])
def test_normalise_digits(spoken, expected):
    assert tools.normalise_digits(spoken) == expected


def test_ambiguous_filler_words_do_not_break_verification():
    assert "1032" in tools.digit_candidates("oh it's one zero three two")
    assert "4421" in tools.digit_candidates("the one ending 4 4 2 1")
    assert verify("cust_003", "oh, it is one zero three two").startswith("VERIFIED")


def test_rupees_in_words():
    assert tools.rupees_in_words(999) == "nine hundred ninety-nine rupees"
    assert tools.rupees_in_words(1499) == "one thousand four hundred ninety-nine rupees"
    assert tools.rupees_in_words(2000) == "two thousand rupees"
    assert tools.rupees_in_words(150000) == "one lakh fifty thousand rupees"


# ------------------------------------------------------------------ verification

def test_payment_details_are_gated_on_verification():
    result, err = run("get_failure_details", customer_id="cust_001")
    assert result == "" and "not verified" in err
    assert verify("cust_001", "0000").startswith("MISMATCH")
    assert verify("cust_001", "double four two one").startswith("VERIFIED")
    result, _ = run("get_failure_details", customer_id="cust_001")
    assert "INR 999" in result and "nine hundred ninety-nine rupees" in result and "insufficient balance" in result


def test_verification_is_per_call():
    assert verify("cust_004", "5590", call_id="call-1").startswith("VERIFIED")
    _, err = run("get_failure_details", "call-2", customer_id="cust_004")
    assert err and "not verified on this call" in err


def test_two_mismatches_lock_the_call_but_not_the_next_one():
    assert verify("cust_001", "1111").startswith("MISMATCH")
    assert verify("cust_001", "2222").startswith("LOCKED")
    assert verify("cust_001", "4421").startswith("LOCKED")           # correct digits too late on this call
    assert "NOT a wrong number" in verify("cust_001", "4421")
    assert verify("cust_001", "4421", call_id="call-B").startswith("VERIFIED")
    assert db.get_customer("cust_001")["state"]["recovery_status"] == "pending"   # still contactable


def test_mismatch_reads_back_what_was_heard():
    assert "0 3 2" in verify("cust_003", "0 3 2")


# ------------------------------------------------------------------ the server never trusts claims

def test_claiming_the_mandate_was_resumed_changes_nothing():
    """Regression for the live call: customer said 'yeah, I did that' and the payment was captured."""
    verify("cust_003", "1032")
    status, _ = run("check_mandate_status", customer_id="cust_003")
    assert status.startswith("PAUSED")
    # even if the model ignores the check and passes a stale 'mandate_resumed' flag, the bank declines
    res, _ = run("retry_payment", customer_id="cust_003", customer_consented=True, mandate_resumed=True)
    assert res.startswith("FAILED") and "still paused" in res
    assert db.get_customer("cust_003")["state"]["recovery_status"] != "recovered"

    tools.customer_action("cust_003", "resume_mandate")              # the customer really does it
    status, _ = run("check_mandate_status", customer_id="cust_003")
    assert status.startswith("ACTIVE")
    res, _ = run("retry_payment", customer_id="cust_003", customer_consented=True)
    assert res.startswith("SUCCESS")
    assert db.get_customer("cust_003")["state"]["recovered_amount_inr"] == 1499


def test_claiming_funds_changes_nothing():
    verify("cust_001", "4421")
    res, _ = run("retry_payment", customer_id="cust_001", customer_consented=True, funds_confirmed=True)
    assert res.startswith("FAILED") and "Do not retry again" in res
    tools.customer_action("cust_001", "add_funds")
    res, _ = run("retry_payment", customer_id="cust_001", customer_consented=True)
    assert res.startswith("SUCCESS")


def test_retry_needs_explicit_consent():
    verify("cust_004", "5590")
    res, _ = run("retry_payment", customer_id="cust_004")
    assert res.startswith("CONSENT NEEDED")
    res, _ = run("retry_payment", customer_id="cust_004", customer_consented=False)
    assert res.startswith("CONSENT NEEDED")
    assert db.get_customer("cust_004")["state"]["debit_attempts"] == 0
    res, _ = run("retry_payment", customer_id="cust_004", customer_consented=True)
    assert res.startswith("SUCCESS")
    st = db.get_customer("cust_004")["state"]
    assert st["recovery_status"] == "recovered" and st["recovered_amount_inr"] == 999 and st["debit_attempts"] == 1


def test_no_double_charge_and_debit_limit_per_call():
    verify("cust_004", "5590")
    run("retry_payment", customer_id="cust_004", customer_consented=True)
    res, _ = run("retry_payment", customer_id="cust_004", customer_consented=True)
    assert res.startswith("ALREADY PAID")

    verify("cust_008", "9174")
    for _ in range(tools.MAX_DEBITS_PER_CALL):
        assert run("retry_payment", customer_id="cust_008", customer_consented=True)[0].startswith("FAILED")
    assert run("retry_payment", customer_id="cust_008", customer_consented=True)[0].startswith("RETRY LIMIT")
    assert db.get_customer("cust_008")["state"]["debit_attempts"] == tools.MAX_DEBITS_PER_CALL


def test_customer_action_validation():
    with pytest.raises(ValueError):
        tools.customer_action("cust_004", "resume_mandate")     # not paused
    with pytest.raises(ValueError):
        tools.customer_action("cust_004", "teleport")


# ------------------------------------------------------------------ other paths

def test_schedule_retry_validates_dates():
    verify("cust_008", "9174")
    assert run("schedule_retry", customer_id="cust_008", retry_date="next tuesday")[0].startswith("INVALID")
    far = (date.today() + timedelta(days=30)).isoformat()
    assert run("schedule_retry", customer_id="cust_008", retry_date=far)[0].startswith("OUT OF RANGE")
    ok = (date.today() + timedelta(days=3)).isoformat()
    assert run("schedule_retry", customer_id="cust_008", retry_date=ok)[0].startswith("SCHEDULED")
    st = db.get_customer("cust_008")["state"]
    assert st["recovery_status"] == "promise_to_pay" and st["funds_available"] is False


def test_link_for_expired_card_sets_new_mandate():
    verify("cust_002", "7788")
    assert run("check_mandate_status", customer_id="cust_002")[0].startswith("INVALID")
    res, _ = run("send_payment_link", customer_id="cust_002", purpose="new_mandate")
    assert "fresh autopay mandate" in res and "+91" not in res      # never read the phone number aloud
    assert db.get_customer("cust_002")["state"]["recovery_status"] == "needs_new_mandate"


def test_do_not_call_is_honoured_without_verification():
    res, err = run("set_contact_preference", customer_id="cust_003", preference="do_not_call")
    assert err is None and res.startswith("RECORDED")
    ok, why = policy.eligibility(db.get_customer("cust_003"), ignore_window=True)
    assert not ok and "do-not-call" in why


def test_wrong_number_is_guarded():
    """Regression for the live call: a real customer was marked as a wrong number after a digit mismatch."""
    verify("cust_001", "4040")
    res, _ = run("mark_wrong_number", customer_id="cust_001", person_denied_being_customer=False, what_they_said="double 4 2 1")
    assert res.startswith("REFUSED")
    res, _ = run("mark_wrong_number", customer_id="cust_001")
    assert res.startswith("REFUSED")
    assert db.get_customer("cust_001")["state"]["recovery_status"] == "pending"

    verify("cust_004", "5590")
    res, _ = run("mark_wrong_number", customer_id="cust_004", person_denied_being_customer=True, what_they_said="x")
    assert res.startswith("REFUSED") and "passed identity verification" in res

    res, _ = run("mark_wrong_number", customer_id="cust_009", person_denied_being_customer=True,
                 what_they_said="This is Rahul, I don't know any Arjun")
    assert res.startswith("RECORDED")
    assert not policy.eligibility(db.get_customer("cust_009"), ignore_window=True)[0]


def test_escalation_is_terminal():
    run("escalate_to_human", customer_id="cust_007", reason="says he cancelled")
    assert not policy.eligibility(db.get_customer("cust_007"), ignore_window=True)[0]


def test_unknown_customer_and_tool():
    _, err = run("verify_identity", customer_id="nope", spoken_digits="1")
    assert "unknown customer" in err
    _, err = run("not_a_tool")
    assert "unknown tool" in err


def test_max_attempts_policy():
    db.update_state("cust_001", attempts=3)
    ok, why = policy.eligibility(db.get_customer("cust_001"), ignore_window=True)
    assert not ok and "max attempts" in why


# ------------------------------------------------------------------ call outcome comes from tool events

def ev(name, result, **args):
    return {"kind": "tool", "name": name, "args": args, "result": result, "error": None}


@pytest.mark.parametrize("events,expected", [
    ([], "no_resolution"),
    ([ev("verify_identity", "MISMATCH. ..."), ev("verify_identity", "LOCKED. ...")], "verification_failed"),
    ([ev("retry_payment", "FAILED. ..."), ev("schedule_retry", "SCHEDULED. ...")], "promise_to_pay"),
    ([ev("retry_payment", "FAILED. ..."), ev("retry_payment", "SUCCESS. ...")], "recovered"),
    ([ev("retry_payment", "SUCCESS. ..."), ev("set_contact_preference", "RECORDED. ...", preference="callback")], "recovered"),
    ([ev("send_payment_link", "SENT. ... It also sets up a fresh autopay mandate ...")], "needs_new_mandate"),
    ([ev("send_payment_link", "SENT. ...")], "link_sent"),
    ([ev("set_contact_preference", "RECORDED. ...", preference="callback")], "callback_requested"),
    ([ev("set_contact_preference", "RECORDED. ...", preference="do_not_call")], "do_not_call"),
    ([ev("escalate_to_human", "ESCALATED. ...")], "escalated"),
    ([ev("mark_wrong_number", "REFUSED. ...")], "no_resolution"),
    ([{"kind": "transcript", "role": "user", "text": "I paid already"}], "no_resolution"),   # claims are not outcomes
])
def test_outcome_from_events(events, expected):
    assert tools.outcome_from_events(events) == expected


def test_no_hangup_tool_so_the_agent_must_speak_the_outcome():
    """Regression for live calls where the agent hung up right after a tool call without confirming anything."""
    from app import config, vapi_client
    config.PUBLIC_BASE_URL = config.PUBLIC_BASE_URL or "https://example.test"
    payload = vapi_client.build_assistant_payload()
    assert all(t["type"] == "function" for t in payload["model"]["tools"])
    assert "goodbye" in payload["endCallPhrases"] and payload["endCallMessage"] == ""
    verify("cust_008", "9174")
    ok = (date.today() + timedelta(days=2)).isoformat()
    res = run("schedule_retry", customer_id="cust_008", retry_date=ok)[0]
    assert "Tell the customer that exact day" in res and "say goodbye" in res


def test_demo_mode_never_bypasses_do_not_call():
    dnc = db.get_customer("cust_006")
    assert policy.eligibility(dnc, demo=True) == (False, "customer opted out (do-not-call)")
    db.update_state("cust_001", attempts=99, recovery_status="recovered")
    assert policy.eligibility(db.get_customer("cust_001"), demo=True)[0]        # replayable in demo mode
    assert not policy.eligibility(db.get_customer("cust_001"), ignore_window=True)[0]   # but not in a real campaign
