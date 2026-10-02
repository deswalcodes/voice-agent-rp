# Design notes

Reference material for the code. The [README](../README.md) covers the what and why; this covers the contracts.

## Contents

- [Principle](#principle)
- [State model](#state-model)
- [Failure catalogue](#failure-catalogue)
- [Tool reference](#tool-reference)
- [Call outcome](#call-outcome)
- [Policy](#policy)
- [Webhook contract](#webhook-contract)
- [The assistant on Vapi](#the-assistant-on-vapi)
- [Outbound phone calls](#outbound-phone-calls)
- [The ten customers](#the-ten-customers)

## Principle

The voice model is treated as an untrusted client of the server. It can ask for things; it cannot assert things.

- It never states a fact about the account that a tool did not return on this call.
- It never converts, compares, or validates data. It relays the customer's words and the server does the work.
- Anything it can do wrong is either impossible (no such tool), refused (the tool checks its preconditions), or visible (every tool call is logged on the call's timeline).

## State model

Each customer has a static record (`data/customers.json`) and mutable state in SQLite (`app/db.py`).

| Field | Meaning | Changed by |
|---|---|---|
| `recovery_status` | `pending`, `failed`, `recovered`, `promise_to_pay`, `link_sent`, `needs_new_mandate`, `escalated`, `do_not_call`, `wrong_number` | Tools |
| `mandate_status` | Bank-side truth: `active`, `paused`, `card_expired`, `limit_exceeded`, `revoked`, `account_frozen` | **Customer actions only** |
| `funds_available` | Bank-side truth: whether a debit would clear | **Customer actions only** |
| `verified_call` | Call id on which identity was verified | `verify_identity` |
| `verify_failures`, `verify_call` | Mismatch count for the current call | `verify_identity` |
| `retries_this_call`, `retry_call` | Debit attempts on the current call (max 2) | `retry_payment` |
| `debit_attempts` | Lifetime debit retries made by the agent | `retry_payment` |
| `attempts` | Contact attempts. Every completed call adds one | End-of-call report |
| `do_not_call` | Opt-out. Checked before every dial | `set_contact_preference`, seed data |
| `payment_link`, `scheduled_retry`, `callback_time`, `escalation` | Artefacts of the chosen recovery path | Tools |

"Customer actions" are what a real customer does on their own phone or bank app: resuming a mandate, adding funds. In the demo they are buttons in the dashboard (`POST /api/customers/{id}/customer-action`). They are the only writers of bank-side truth, and they are logged on the open call's timeline so a reviewer can see why a later tool result changed.

A merchant cannot see a customer's balance. So for insufficient funds there is no pre-check: the agent asks, gets consent, and the debit result is the answer.

## Failure catalogue

`app/payments.py` maps each failure reason to how it is explained and how it can be recovered.

| Reason | Said to the customer as | Retry can work? | Path |
|---|---|---|---|
| `bank_server_down` | The bank's servers were unavailable; nothing wrong on your side | Yes | Retry now |
| `technical_decline` | A temporary technical error at the bank | Yes | Retry now |
| `insufficient_funds` | The bank reported insufficient balance | Yes, once funds exist | Retry with consent, or schedule a date |
| `mandate_paused` | The mandate is paused in your UPI app | Yes, once resumed | Resume, verify status, retry |
| `card_expired` | The card on the mandate has expired | No | New-mandate link |
| `mandate_limit_exceeded` | The plan amount is above your mandate limit | No | New-mandate link |
| `mandate_revoked` | The mandate was cancelled | No | Clarify intent; link or escalate |
| `account_frozen` | The bank has a hold on the account | No | Link, pay from another account |

`retry_autopay` takes only bank-side state as input. It has no parameter through which a claim could reach it.

## Tool reference

All handlers are in `app/tools.py`. Each returns one short string, which is what the model reads. Results start with an upper-case status word so both the model and the outcome logic can branch on it.

| Tool | Requires | Returns | Notes |
|---|---|---|---|
| `verify_identity(spoken_digits)` | nothing | `VERIFIED`, `MISMATCH` (with the digits heard), `LOCKED` | Words passed verbatim. Server parses English and Hindi number words, Devanagari numerals, "double"/"triple". Ambiguous filler ("oh", "one", Hindi "do") is tried both ways. Two mismatches lock the call; the customer stays contactable |
| `get_failure_details()` | verified | Plan, amount in digits and in words, method, reason, recommended path | Amount in words exists so the speech engine reads it correctly |
| `check_mandate_status()` | verified | `ACTIVE`, `PAUSED`, `INVALID`, `REVOKED`, `BLOCKED` | Reads bank-side state. Required before retrying when a customer says they fixed the mandate |
| `retry_payment(customer_consented)` | verified, consent | `SUCCESS`, `FAILED`, `CONSENT NEEDED`, `RETRY LIMIT`, `ALREADY PAID` | Max two debits per call. Never charges after a capture |
| `send_payment_link(purpose)` | verified | `SENT` | `new_mandate` or `one_time_payment`. The phone number is never read aloud |
| `schedule_retry(retry_date, note)` | verified | `SCHEDULED`, `INVALID DATE`, `OUT OF RANGE` | Today to today + 10 days |
| `set_contact_preference(preference, callback_time)` | nothing | `RECORDED` | `do_not_call` or `callback`. Deliberately ungated: an opt-out must always work |
| `escalate_to_human(reason)` | nothing | `ESCALATED` | Stops automatic retries |
| `mark_wrong_number(person_denied_being_customer, what_they_said)` | nothing | `RECORDED`, `REFUSED` | Refused unless the person denied being the customer. Refused outright if they passed verification on this call |

There is no hang-up tool. The assistant is configured with `endCallPhrases: ["goodbye", "good bye"]`, so the only way to end a call is to speak, and every terminal tool result tells the agent what to say first.

## Call outcome

`tools.outcome_from_events` walks the call's tool events and returns one of `recovered`, `promise_to_pay`, `link_sent`, `needs_new_mandate`, `escalated`, `do_not_call`, `callback_requested`, `wrong_number`, `verification_failed`, `no_resolution`. The last state-changing tool wins, except that nothing overrides `recovered`.

Vapi's post-call analysis (summary, structured data, pass/fail conduct evaluation) is stored alongside it and shown in the dashboard, but it only reads spoken lines. When the two disagree, the tool-derived outcome is correct by construction.

## Policy

`app/policy.py`, checked before any call is placed:

1. Do-not-call. Never bypassed, including in the dashboard's demo mode.
2. Terminal states (`recovered`, `escalated`, `wrong_number`, `do_not_call`).
3. Contact attempts at or above `MAX_ATTEMPTS` (default 3).
4. Calling window, default 09:00 to 20:00 `Asia/Kolkata`. A configurable default that reflects customer-contact norms; it is not legal advice.

The dashboard's *Call in browser* runs in demo mode, which skips 2 to 4 so a scenario can be replayed. The campaign script and the *Campaign plan* view apply all four and print the reason for every skip.

## Webhook contract

`POST /vapi/webhook`, authenticated by the `x-vapi-secret` header.

| `message.type` | Handling |
|---|---|
| `tool-calls` | Each call is dispatched and answered as `{"results": [{"toolCallId", "name", "result" \| "error"}]}`. Both payload shapes Vapi uses are accepted (`parameters` object, or `function.arguments` as a JSON string). The customer id falls back to the call's `variableValues` if the model omits it |
| `transcript` | Final lines are stored on the call timeline |
| `status-update` | Call status tracked |
| `end-of-call-report` | Stores transcript, duration, cost, and the LLM analysis; computes the outcome from tool events; counts one contact attempt |

Tool errors are returned to the model as `error` strings, never raised, so the agent can recover in conversation.

## The assistant on Vapi

`scripts/setup_assistant.py` builds the assistant from code and creates or updates it (`--print` shows the payload without sending it).

- Model and voice run on Vapi's provider keys: `gpt-4.1-mini`, Vapi voice `Naina`, Deepgram `nova-3`. All are set in `.env`.
- Per-call variables (`customer_id`, name, language, what to verify, today's date) are injected through `assistantOverrides.variableValues`.
- Customers whose preferred language is Hinglish get Deepgram's multilingual mode.
- The prompt asks for Latin script only and amounts in words, because that is what the speech engine reads well.

## Outbound phone calls

Free Vapi numbers are inbound-only. Import a Twilio, Telnyx, or Vonage number in the Vapi dashboard, set `VAPI_PHONE_NUMBER_ID`, then:

```bash
python -m scripts.run_campaign --to +91XXXXXXXXXX            # full policy applied
python -m scripts.run_campaign --to +91XXXXXXXXXX --force    # ignore the calling window
python -m scripts.run_campaign --to +91XXXXXXXXXX --only cust_004
```

Every call goes to the one number passed with `--to`. The fictional numbers in the customer file are never dialled. Use only a number you own or have explicit permission to call.

## The ten customers

All fictional, all subscribers of one fictional merchant, StreamBox. Numbers on file are placeholders in the `+91 90000 000xx` range.

| id | Name | Failure | Expected handling |
|---|---|---|---|
| cust_001 | Aarav Mehta | Insufficient funds | Consent, retry; bank declines until funds are really added |
| cust_002 | Priya Sharma | Card expired | No retry; new-mandate link |
| cust_003 | Rohan Verma (Hinglish) | Mandate paused | Explain how to resume, check status, retry |
| cust_004 | Sneha Iyer | Bank server down | Reassure, consent, retry |
| cust_005 | Vikram Singh (Hinglish) | Mandate limit below new price | New-mandate link; note the price-change complaint |
| cust_006 | Ananya Reddy | Insufficient funds, do-not-call | Never dialled |
| cust_007 | Karthik Nair | Mandate revoked, says he cancelled | No retry, no argument; escalate |
| cust_008 | Meera Joshi | Insufficient funds until salary day | Promise to pay; schedule the retry |
| cust_009 | Arjun Kapoor | Number reassigned | Disclose nothing; mark wrong number |
| cust_010 | Divya Pillai | Account on hold | Link, pay from another account |
