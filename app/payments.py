"""Mock payment gateway for autopay (recurring mandate) retries.

Models the failure codes a Razorpay-style subscriptions/mandate stack surfaces and
what each one needs before a retry can succeed. Deterministic on purpose so the
demo and tests are reproducible.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

# What the customer hears vs. what the ops team sees.
FAILURE_CATALOG: dict[str, dict[str, Any]] = {
    "insufficient_funds": {
        "customer_facing": "the bank reported insufficient balance at the time of the debit",
        "recovery_path": "retry_after_funds",
        "retry_possible": True,
        "needs_new_mandate": False,
    },
    "bank_server_down": {
        "customer_facing": "the bank's servers were temporarily unavailable, nothing was wrong on your side",
        "recovery_path": "retry_now",
        "retry_possible": True,
        "needs_new_mandate": False,
    },
    "technical_decline": {
        "customer_facing": "a temporary technical error at the bank",
        "recovery_path": "retry_now",
        "retry_possible": True,
        "needs_new_mandate": False,
    },
    "mandate_paused": {
        "customer_facing": "the autopay mandate is currently paused in your UPI app",
        "recovery_path": "resume_mandate_then_retry",
        "retry_possible": True,
        "needs_new_mandate": False,
    },
    "card_expired": {
        "customer_facing": "the card linked to the mandate has expired",
        "recovery_path": "new_mandate_via_link",
        "retry_possible": False,
        "needs_new_mandate": True,
    },
    "mandate_limit_exceeded": {
        "customer_facing": "the plan amount is now higher than the limit set on your autopay mandate",
        "recovery_path": "new_mandate_via_link",
        "retry_possible": False,
        "needs_new_mandate": True,
    },
    "mandate_revoked": {
        "customer_facing": "the autopay mandate was cancelled from the bank or UPI app side",
        "recovery_path": "clarify_intent_then_link_or_close",
        "retry_possible": False,
        "needs_new_mandate": True,
    },
    "account_frozen": {
        "customer_facing": "the bank has put a hold on the linked account",
        "recovery_path": "pay_from_other_account_via_link",
        "retry_possible": False,
        "needs_new_mandate": True,
    },
}


@dataclass
class RetryResult:
    success: bool
    code: str
    message: str
    payment_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"success": self.success, "code": self.code, "message": self.message,
                "payment_id": self.payment_id, **self.extra}


def _pid(seed: str) -> str:
    return "pay_" + hashlib.sha1(seed.encode()).hexdigest()[:12]


def describe_failure(reason: str) -> dict[str, Any]:
    return FAILURE_CATALOG.get(reason, {
        "customer_facing": "a payment failure",
        "recovery_path": "retry_now",
        "retry_possible": True,
        "needs_new_mandate": False,
    })


MANDATE_STATUS_TEXT = {
    "active": "ACTIVE. The mandate is live and can be debited.",
    "paused": "PAUSED. The mandate is still paused on the bank/UPI side; a debit will be declined until the customer resumes it.",
    "card_expired": "INVALID. The card behind the mandate has expired; a new mandate is required.",
    "limit_exceeded": "INVALID FOR THIS AMOUNT. The plan amount is above the mandate limit; a new mandate is required.",
    "revoked": "REVOKED. The mandate was cancelled; a new mandate is required if the customer wants to continue.",
    "account_frozen": "BLOCKED. The bank has a hold on the linked account; payment must come from another account.",
}


def mandate_status_text(status: str) -> str:
    return MANDATE_STATUS_TEXT.get(status, f"UNKNOWN ({status}).")


def retry_autopay(customer: dict[str, Any], *, mandate_status: str, funds_available: bool, attempt_no: int) -> RetryResult:
    """Attempt a re-presentment of the failed mandate debit.

    The result depends only on the bank-side ground truth (mandate status and
    balance), exactly like a real gateway. What the customer claimed on the call
    is irrelevant here.
    """
    reason = customer["failure_reason"]
    amount = customer["amount_inr"]
    info = describe_failure(reason)
    seed = f"{customer['id']}:{attempt_no}"

    if info["needs_new_mandate"] or mandate_status not in ("active", "paused"):
        return RetryResult(False, reason,
                           f"Retry blocked: {info['customer_facing']}. A fresh mandate or one-time payment link is required.")
    if mandate_status == "paused":
        return RetryResult(False, "mandate_paused",
                           "Declined: the mandate is still paused on the bank side. The customer must resume it in their UPI app (Autopay > Manage > Resume).")
    if not funds_available:
        return RetryResult(False, "insufficient_funds", "Declined again by the bank: insufficient balance.")
    return RetryResult(True, "captured", f"INR {amount} captured.", _pid(seed))


def create_payment_link(customer: dict[str, Any], *, purpose: str) -> dict[str, Any]:
    """Create a one-time payment link that also sets up a fresh mandate where needed."""
    token = hashlib.sha1(f"link:{customer['id']}:{purpose}".encode()).hexdigest()[:10]
    needs_mandate = describe_failure(customer["failure_reason"])["needs_new_mandate"]
    return {
        "link_id": f"plink_{token}",
        "url": f"https://rzp.io/l/{token}",          # fictional short link
        "amount_inr": customer["amount_inr"],
        "sets_up_new_mandate": needs_mandate,
        "expires_in_hours": 48,
    }
