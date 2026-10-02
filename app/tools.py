"""Tool definitions (sent to Vapi) and their server-side handlers.

Design rule: the server never trusts what the customer *says* happened. Identity,
mandate status and debit outcomes all come from records the server owns. The
model's job is to talk; the tools decide.

Every handler returns a short, flat string: that is what the voice model reads
back, so it should be something an agent can paraphrase in one breath.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import db, payments

MAX_VERIFY_TRIES = 2          # per call
MAX_DEBITS_PER_CALL = 2       # each failed debit can cost the customer a bank fee


@dataclass
class Ctx:
    customer_id: str | None = None   # from the call's variableValues, used when the model omits customer_id
    call_id: str | None = None

    @property
    def call_key(self) -> str:
        return self.call_id or "_no_call_"


# ---------------------------------------------------------------------------
# Spoken-number handling
# ---------------------------------------------------------------------------

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "shunya": "0", "sunya": "0", "sifar": "0", "शून्य": "0", "ज़ीरो": "0", "जीरो": "0",
    "one": "1", "ek": "1", "एक": "1", "वन": "1",
    "two": "2", "do": "2", "दो": "2", "टू": "2",
    "three": "3", "teen": "3", "tin": "3", "तीन": "3", "थ्री": "3",
    "four": "4", "char": "4", "chaar": "4", "चार": "4", "फोर": "4",
    "five": "5", "paanch": "5", "panch": "5", "पांच": "5", "पाँच": "5", "फाइव": "5",
    "six": "6", "chhe": "6", "che": "6", "chhah": "6", "छह": "6", "छः": "6", "छे": "6", "सिक्स": "6",
    "seven": "7", "saat": "7", "sat": "7", "सात": "7", "सेवन": "7",
    "eight": "8", "aath": "8", "ath": "8", "आठ": "8", "एट": "8",
    "nine": "9", "nau": "9", "नौ": "9", "नाइन": "9",
}
_MULTIPLIERS = {"double": 2, "डबल": 2, "triple": 3, "ट्रिपल": 3}
_DEVANAGARI_NUMERALS = str.maketrans("०१२३४५६७८९", "0123456789")
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]


# Words that are a digit in one language and filler in another ("oh, it's the one ending...", Hindi "do" = 2).
_AMBIGUOUS = {"o", "oh", "do", "one"}


def digit_candidates(spoken: str) -> list[str]:
    """Every plausible digit string for what was said; the first is the literal reading.

    'double 4 2 1' -> ['4421'];  'एक 0 3 2' -> ['1032'];  '44-21' -> ['4421']
    'oh it is one zero three two' -> ['01032', '1032', '0032', '032']
    """
    text = str(spoken or "").lower().translate(_DEVANAGARI_NUMERALS)
    parts: list[tuple[str, bool]] = []          # (digits, is_ambiguous)
    mult = 1
    # ASCII digit runs, Latin words, or Devanagari words (kept whole, including vowel signs; dandas excluded)
    for tok in re.findall(r"\d+|[a-z]+|[ऀ-ॣ॰-ॿ]+", text):
        if tok in _MULTIPLIERS:
            mult = _MULTIPLIERS[tok]
            continue
        digit = tok if tok.isdigit() else _DIGIT_WORDS.get(tok)
        if digit is None:
            continue                            # filler such as "it's", "the", "number"
        parts.append((digit * mult if len(digit) == 1 else digit, tok in _AMBIGUOUS and mult == 1))
        mult = 1
    amb = [i for i, (_, a) in enumerate(parts) if a][:6]
    out: list[str] = []
    for mask in range(1 << len(amb)):
        dropped = {amb[j] for j in range(len(amb)) if mask >> j & 1}
        cand = "".join(d for i, (d, _) in enumerate(parts) if i not in dropped)
        if cand not in out:
            out.append(cand)
    return out


def normalise_digits(spoken: str) -> str:
    """Best single reading: a 4-digit candidate if there is one, else the literal reading."""
    cands = digit_candidates(spoken)
    return next((c for c in cands if len(c) == 4), cands[0])


def rupees_in_words(n: int) -> str:
    """1499 -> 'one thousand four hundred ninety-nine rupees' so the TTS reads amounts naturally."""
    def below_100(x: int) -> str:
        if x < 20:
            return _ONES[x]
        return _TENS[x // 10] + ("-" + _ONES[x % 10] if x % 10 else "")

    def below_1000(x: int) -> str:
        h, r = divmod(x, 100)
        parts = ([_ONES[h] + " hundred"] if h else []) + ([below_100(r)] if r or not h else [])
        return " ".join(parts)

    n = int(n)
    lakh, rest = divmod(n, 100000)
    thousand, rest = divmod(rest, 1000)
    parts = []
    if lakh:
        parts.append(below_100(lakh) + " lakh")
    if thousand:
        parts.append(below_100(thousand) + " thousand")
    if rest or not parts:
        parts.append(below_1000(rest))
    return " ".join(parts) + " rupees"


def _amount(cust: dict[str, Any]) -> str:
    return f"INR {cust['amount_inr']} (say it as: {rupees_in_words(cust['amount_inr'])})"


# ---------------------------------------------------------------------------
# Definitions. Vapi wraps these as {"type": "function", "function": {...}}.
# ---------------------------------------------------------------------------

def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


CUSTOMER_ID = {"type": "string", "description": "The customer_id given in the system prompt."}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "verify_identity",
        "Check the 4 verification digits the customer said. Call it every time the customer gives digits. The server does the comparison and number conversion; you only relay what you heard.",
        {"customer_id": CUSTOMER_ID,
         "spoken_digits": {"type": "string", "description": "The customer's words for the digits, verbatim, in any language or script, e.g. 'double four two one', '1 0 3 2', 'एक शून्य तीन दो'. Do not convert, shorten, or drop anything."}},
        ["customer_id", "spoken_digits"],
    ),
    _tool(
        "get_failure_details",
        "Fetch why the autopay failed and the recommended recovery path. Only works after verify_identity returned VERIFIED on this call.",
        {"customer_id": CUSTOMER_ID},
        ["customer_id"],
    ),
    _tool(
        "check_mandate_status",
        "Look up the live status of the autopay mandate at the bank. Always call this when the customer says they resumed, fixed, or re-enabled the mandate: never take their word for it.",
        {"customer_id": CUSTOMER_ID},
        ["customer_id"],
    ),
    _tool(
        "retry_payment",
        "Attempt the debit again now. Only after the customer explicitly agreed to be charged now. The bank decides the outcome; report exactly what this returns.",
        {"customer_id": CUSTOMER_ID,
         "customer_consented": {"type": "boolean", "description": "True only if the customer clearly said yes to being debited right now on this call."}},
        ["customer_id", "customer_consented"],
    ),
    _tool(
        "send_payment_link",
        "Send a secure payment link by SMS so the customer can pay from any account and, if needed, set up a fresh mandate. Use when a retry is not possible, a retry failed, or the customer prefers a link.",
        {"customer_id": CUSTOMER_ID,
         "purpose": {"type": "string", "enum": ["new_mandate", "one_time_payment"],
                      "description": "new_mandate when the card/mandate must be replaced, else one_time_payment."}},
        ["customer_id", "purpose"],
    ),
    _tool(
        "schedule_retry",
        "Schedule the next automatic retry on a date the customer commits to (promise to pay).",
        {"customer_id": CUSTOMER_ID,
         "retry_date": {"type": "string", "description": "ISO date YYYY-MM-DD, must be today or later and within 10 days. Today's date is in the system prompt."},
         "note": {"type": "string", "description": "The customer's own stated reason, if they gave one. Leave empty otherwise; never invent one."}},
        ["customer_id", "retry_date"],
    ),
    _tool(
        "set_contact_preference",
        "Record that the customer does not want calls, or wants a callback at a specific time. Always honour this immediately.",
        {"customer_id": CUSTOMER_ID,
         "preference": {"type": "string", "enum": ["do_not_call", "callback"]},
         "callback_time": {"type": "string", "description": "When to call back, free text, only for callback."}},
        ["customer_id", "preference"],
    ),
    _tool(
        "escalate_to_human",
        "Hand the case to a human support agent. Use for disputes, claims of cancellation, refund requests, or anything you cannot resolve. Never argue with the customer.",
        {"customer_id": CUSTOMER_ID,
         "reason": {"type": "string", "description": "One sentence on what the customer is claiming or asking."}},
        ["customer_id", "reason"],
    ),
    _tool(
        "mark_wrong_number",
        "Use ONLY when the person who answered says they are not the named customer or do not know them. Never use it because verification digits did not match, because the customer forgot the digits, or just to end a call.",
        {"customer_id": CUSTOMER_ID,
         "person_denied_being_customer": {"type": "boolean", "description": "True only if the person explicitly said they are not the named customer."},
         "what_they_said": {"type": "string", "description": "Their words, e.g. 'No, this is Rahul, I don't know any Arjun'."}},
        ["customer_id", "person_denied_being_customer", "what_they_said"],
    ),
]


def vapi_tools(server_url: str, secret: str | None) -> list[dict[str, Any]]:
    server: dict[str, Any] = {"url": server_url, "timeoutSeconds": 20}
    if secret:
        server["secret"] = secret
    tools: list[dict[str, Any]] = [
        {"type": "function", "function": t, "server": server, "async": False}
        for t in TOOL_DEFINITIONS
    ]
    # No endCall tool on purpose: the call ends when the agent SAYS goodbye (endCallPhrases), so it
    # cannot hang up silently right after a tool call without telling the customer the outcome.
    return tools


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _need_customer(args: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    cid = (args.get("customer_id") or ctx.customer_id or "").strip()
    cust = db.get_customer(cid)
    if not cust:
        raise ValueError(f"unknown customer_id '{cid}'")
    return cust


def is_verified(cust: dict[str, Any], ctx: Ctx) -> bool:
    return cust["state"].get("verified_call") == ctx.call_key


def _require_verified(cust: dict[str, Any], ctx: Ctx) -> None:
    if not is_verified(cust, ctx):
        raise PermissionError("Identity not verified on this call. Ask for the 4 digits and call verify_identity first.")


LOCKED_MSG = ("LOCKED. Verification failed twice, so nothing about the account may be shared on this call. "
              "This is NOT a wrong number: do not call mark_wrong_number. Apologise, tell them no changes were made and "
              "that they can reach support from the app, then say goodbye.")


def h_verify_identity(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    st = cust["state"]
    fails = st.get("verify_failures", 0) if st.get("verify_call") == ctx.call_key else 0
    if is_verified(cust, ctx):
        return "VERIFIED already on this call. Continue."
    if fails >= MAX_VERIFY_TRIES:
        return LOCKED_MSG
    said = str(args.get("spoken_digits") or args.get("last4") or "")
    digits = normalise_digits(said)
    if cust["verify_last4"] in digit_candidates(said):
        db.update_state(cust["id"], verified_call=ctx.call_key, verify_call=ctx.call_key, verify_failures=0)
        return "VERIFIED. You may now discuss the payment. Next call get_failure_details."
    fails += 1
    db.update_state(cust["id"], verify_call=ctx.call_key, verify_failures=fails,
                    last_outcome="verification_failed" if fails >= MAX_VERIFY_TRIES else st.get("last_outcome"))
    if fails >= MAX_VERIFY_TRIES:
        return LOCKED_MSG
    heard = " ".join(digits) if digits else "no digits"
    return (f"MISMATCH. The server heard: {heard}. Share no payment detail. Read those digits back, ask the customer "
            f"to say all 4 digits again slowly, then call verify_identity with their exact words. One more try is allowed.")


def h_get_failure_details(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    _require_verified(cust, ctx)
    info = payments.describe_failure(cust["failure_reason"])
    return (
        f"Plan: {cust['plan']}. Amount: {_amount(cust)}. Method: {cust['mandate_type']}. Failed on {cust['failed_on']}. "
        f"Reason (say it in plain words): {info['customer_facing']}. "
        f"Recommended path: {info['recovery_path'].replace('_', ' ')}. "
        f"Retry possible: {'yes, once the cause is fixed and the customer consents' if info['retry_possible'] else 'no, a payment link is needed'}."
    )


def h_check_mandate_status(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    _require_verified(cust, ctx)
    status = cust["state"].get("mandate_status", "active")
    text = payments.mandate_status_text(status)
    if status == "active":
        return f"{text} You may ask for consent and retry the payment."
    if status == "paused":
        return (f"{text} Tell the customer it still shows as paused on the bank's side and ask them to complete the "
                f"resume step (UPI app > Autopay > Manage > Resume), then check again. If they cannot do it now, offer a payment link.")
    return f"{text} Do not retry. Offer a payment link."


def h_retry_payment(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    _require_verified(cust, ctx)
    st = cust["state"]
    if st["recovery_status"] == "recovered":
        return "ALREADY PAID. This payment was already captured. Do not charge again. Tell the customer it is already paid, then say goodbye."
    if args.get("customer_consented") is not True:
        return "CONSENT NEEDED. Ask the customer clearly whether you may attempt the debit right now, and only retry on a yes."
    used = st.get("retries_this_call", 0) if st.get("retry_call") == ctx.call_key else 0
    if used >= MAX_DEBITS_PER_CALL:
        return ("RETRY LIMIT. No more debit attempts on this call, because repeated declines can cost the customer bank charges. "
                "Offer a retry date (schedule_retry) or a payment link (send_payment_link).")
    attempt_no = st.get("debit_attempts", 0) + 1
    res = payments.retry_autopay(cust, mandate_status=st.get("mandate_status", "active"),
                                 funds_available=bool(st.get("funds_available")), attempt_no=attempt_no)
    changes: dict[str, Any] = {"debit_attempts": attempt_no, "retry_call": ctx.call_key,
                               "retries_this_call": used + 1, "last_outcome": res.code}
    if res.success:
        changes.update(recovery_status="recovered", recovered_amount_inr=cust["amount_inr"], payment_id=res.payment_id)
        db.update_state(cust["id"], **changes)
        return (f"SUCCESS. {_amount(cust)} captured, payment id {res.payment_id}. "
                f"Tell the customer the payment went through and their service continues uninterrupted, then say goodbye.")
    if st["recovery_status"] == "pending":
        changes["recovery_status"] = "failed"
    db.update_state(cust["id"], **changes)
    if res.code == "insufficient_funds":
        return (f"FAILED. {res.message} Tell the customer honestly that the bank declined it. Do not retry again on this call. "
                f"Offer a retry date (schedule_retry) or a payment link they can pay from another account.")
    if res.code == "mandate_paused":
        return f"FAILED. {res.message} Ask them to resume it and use check_mandate_status before any further retry, or offer a payment link."
    return f"FAILED. {res.message} Offer send_payment_link."


def h_send_payment_link(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    _require_verified(cust, ctx)
    purpose = args.get("purpose") or "one_time_payment"
    link = payments.create_payment_link(cust, purpose=purpose)
    status = "needs_new_mandate" if link["sets_up_new_mandate"] else "link_sent"
    db.update_state(cust["id"], payment_link=link, recovery_status=status, last_outcome="link_sent")
    mandate_note = " It also sets up a fresh autopay mandate so this does not repeat." if link["sets_up_new_mandate"] else ""
    return (f"SENT. An SMS with a secure link for {_amount(cust)} went to the customer's registered mobile; it expires in "
            f"{link['expires_in_hours']} hours.{mandate_note} Tell the customer to look for the SMS, and that you will "
            f"never ask for an OTP or card details on a call.")


def h_schedule_retry(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    _require_verified(cust, ctx)
    raw = str(args.get("retry_date", "")).strip()
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return "INVALID DATE. Ask for a specific day and pass it as YYYY-MM-DD."
    today = date.today()
    if d < today or (d - today).days > 10:
        return (f"OUT OF RANGE. Today is {today.isoformat()}; the retry date must be between today and 10 days from now. "
                f"Offer the nearest valid date.")
    db.update_state(cust["id"], scheduled_retry=d.isoformat(), recovery_status="promise_to_pay",
                    last_outcome="promise_to_pay", promise_note=str(args.get("note", "")))
    return (f"SCHEDULED. The autopay will be retried on {d.strftime('%A, %d %B')}. Tell the customer that exact day and "
            f"that their service stays active until then, ask if there is anything else, then say goodbye.")


def h_set_contact_preference(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    pref = args.get("preference")
    if pref == "do_not_call":
        db.update_state(cust["id"], do_not_call=True, recovery_status="do_not_call", last_outcome="do_not_call")
        return "RECORDED. The customer will not be called again. Tell them that, add that they can reach support anytime, then say goodbye."
    when = str(args.get("callback_time", "")).strip() or "unspecified"
    db.update_state(cust["id"], callback_time=when, last_outcome="callback")
    return f"RECORDED. A callback is noted for: {when}. Say that time back to the customer so they know it is booked, then say goodbye."


def h_escalate_to_human(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    reason = str(args.get("reason", "")).strip() or "customer requested escalation"
    db.update_state(cust["id"], escalation=reason, recovery_status="escalated", last_outcome="escalated")
    return ("ESCALATED. A ticket is open with support; they will reach out within one business day and no further "
            "automatic retries will run meanwhile. Tell the customer exactly that, apologise for the inconvenience, then say goodbye.")


def h_mark_wrong_number(args: dict[str, Any], ctx: Ctx) -> str:
    cust = _need_customer(args, ctx)
    if is_verified(cust, ctx):
        return "REFUSED. This person passed identity verification on this call, so it is not a wrong number. Continue helping them."
    if args.get("person_denied_being_customer") is not True:
        return ("REFUSED. Not a wrong number unless the person says they are not the named customer. "
                "If verification failed, apologise and say goodbye instead.")
    db.update_state(cust["id"], recovery_status="wrong_number", last_outcome="wrong_number",
                    wrong_number_note=str(args.get("what_they_said", ""))[:200])
    return "RECORDED. Apologise for the disturbance and say goodbye. Do not mention any name, amount, or company detail."


HANDLERS: dict[str, Callable[[dict[str, Any], Ctx], str]] = {
    "verify_identity": h_verify_identity,
    "get_failure_details": h_get_failure_details,
    "check_mandate_status": h_check_mandate_status,
    "retry_payment": h_retry_payment,
    "send_payment_link": h_send_payment_link,
    "schedule_retry": h_schedule_retry,
    "set_contact_preference": h_set_contact_preference,
    "escalate_to_human": h_escalate_to_human,
    "mark_wrong_number": h_mark_wrong_number,
}


def dispatch(name: str, args: dict[str, Any] | str | None, fallback_customer_id: str | None,
             call_id: str | None = None) -> tuple[str, str | None]:
    """Run a tool. Returns (result, error). Exactly one is non-empty."""
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return "", f"could not parse arguments for {name}"
    args = args or {}
    handler = HANDLERS.get(name)
    if not handler:
        return "", f"unknown tool {name}"
    try:
        return handler(args, Ctx(customer_id=fallback_customer_id, call_id=call_id)), None
    except (ValueError, PermissionError) as e:
        return "", str(e)


# ---------------------------------------------------------------------------
# Customer-side actions (what the real customer does on their own phone/bank).
# These are the ONLY things that change gateway ground truth.
# ---------------------------------------------------------------------------

def customer_action(customer_id: str, action: str) -> dict[str, Any]:
    cust = db.get_customer(customer_id)
    if not cust:
        raise ValueError(f"unknown customer_id '{customer_id}'")
    st = cust["state"]
    if action == "resume_mandate":
        if st.get("mandate_status") != "paused":
            raise ValueError("mandate is not paused, nothing to resume")
        return db.update_state(customer_id, mandate_status="active")
    if action == "add_funds":
        return db.update_state(customer_id, funds_available=True)
    raise ValueError(f"unknown action '{action}'")


# ---------------------------------------------------------------------------
# Call outcome, derived from what the tools actually did on the call.
# The post-call LLM analysis only sees spoken lines; this is the source of truth.
# ---------------------------------------------------------------------------

def outcome_from_events(events: list[dict[str, Any]]) -> str:
    outcome = "no_resolution"
    for e in events:
        if e.get("kind") != "tool" or e.get("error"):
            continue
        name, res, args = e.get("name"), str(e.get("result") or ""), e.get("args") or {}
        if outcome == "recovered":
            break                                   # nothing later on the call un-pays a captured payment
        if name == "verify_identity" and res.startswith("LOCKED"):
            outcome = "verification_failed"
        elif name == "retry_payment" and res.startswith(("SUCCESS", "ALREADY PAID")):
            outcome = "recovered"
        elif name == "schedule_retry" and res.startswith("SCHEDULED"):
            outcome = "promise_to_pay"
        elif name == "send_payment_link" and res.startswith("SENT"):
            outcome = "needs_new_mandate" if "fresh autopay mandate" in res else "link_sent"
        elif name == "escalate_to_human" and res.startswith("ESCALATED"):
            outcome = "escalated"
        elif name == "set_contact_preference" and res.startswith("RECORDED"):
            outcome = "do_not_call" if args.get("preference") == "do_not_call" else "callback_requested"
        elif name == "mark_wrong_number" and res.startswith("RECORDED"):
            outcome = "wrong_number"
    return outcome
