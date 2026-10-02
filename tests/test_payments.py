from app import payments


def cust(reason, amount=999):
    return {"id": "x", "failure_reason": reason, "amount_inr": amount}


def retry(reason, *, mandate="active", funds=True, n=1):
    return payments.retry_autopay(cust(reason), mandate_status=mandate, funds_available=funds, attempt_no=n)


def test_bank_down_recovers_on_plain_retry():
    r = retry("bank_server_down")
    assert r.success and r.payment_id.startswith("pay_")


def test_insufficient_funds_depends_on_actual_balance_only():
    assert not retry("insufficient_funds", funds=False).success
    assert retry("insufficient_funds", funds=False).code == "insufficient_funds"
    assert retry("insufficient_funds", funds=True).success


def test_paused_mandate_depends_on_actual_mandate_status_only():
    r = retry("mandate_paused", mandate="paused")
    assert not r.success and r.code == "mandate_paused"
    assert retry("mandate_paused", mandate="active").success


def test_new_mandate_reasons_block_retry_whatever_the_state():
    for reason in ("card_expired", "mandate_limit_exceeded", "mandate_revoked", "account_frozen"):
        assert not retry(reason, mandate="active", funds=True).success, reason
        link = payments.create_payment_link(cust(reason), purpose="new_mandate")
        assert link["sets_up_new_mandate"] and link["url"].startswith("https://rzp.io/l/")


def test_payment_ids_are_deterministic():
    assert retry("technical_decline").payment_id == retry("technical_decline").payment_id


def test_mandate_status_text():
    assert payments.mandate_status_text("active").startswith("ACTIVE")
    assert payments.mandate_status_text("paused").startswith("PAUSED")
