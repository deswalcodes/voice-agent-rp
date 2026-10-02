"""Offline end-to-end dry run: replays scripted calls through the real webhook handler.

No Vapi account, no network, no credits. Each scenario sends the exact webhook
shapes Vapi sends (tool-calls, transcript, status-update, end-of-call-report), so
the tool layer, state machine, policy checks, and dashboard all get exercised.

Step kinds:
  ("assistant" | "user", text)        a transcript line
  ("tool", (name, args))              a tool call the model makes
  ("action", name)                    something the CUSTOMER does on their own phone
                                      (resume_mandate / add_funds). Only these change
                                      bank-side truth; what the customer says never does.

    python -m scripts.dry_run            # populate the dashboard with simulated calls
    python -m scripts.dry_run --reset    # wipe state first
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app import config, db, policy
from app.server import app

PROMISE_DATE = (date.today() + timedelta(days=5)).isoformat()


def hook(client: TestClient, message: dict[str, Any]):
    """POST one Vapi-shaped webhook message, authenticated the way Vapi would be."""
    headers = {"x-vapi-secret": config.VAPI_WEBHOOK_SECRET} if config.VAPI_WEBHOOK_SECRET else {}
    return client.post("/vapi/webhook", json={"message": message}, headers=headers)


HELLO = "Hi, this is Asha, an automated assistant calling from StreamBox. Am I speaking with {}?"
ASK = "I'm calling about your StreamBox subscription. For a quick check, could you tell me the {}?"
BANK4 = "last 4 digits of the bank account linked to the UPI mandate"
CARD4 = "last 4 digits of the card on file"

SCENARIOS: dict[str, dict[str, Any]] = {
    "cust_001": {   # claims funds, bank declines, then really adds funds
        "steps": [
            ("assistant", HELLO.format("Aarav Mehta")),
            ("user", "Yeah, speaking. What is this about?"),
            ("assistant", ASK.format(BANK4)),
            ("user", "Uh, 4 0 4 0."),
            ("tool", ("verify_identity", {"customer_id": "cust_001", "spoken_digits": "4 0 4 0"})),
            ("assistant", "I heard four, zero, four, zero and that doesn't match. Could you say all four digits again slowly?"),
            ("user", "Sorry. Double four two one."),
            ("tool", ("verify_identity", {"customer_id": "cust_001", "spoken_digits": "double four two one"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_001"})),
            ("assistant", "Thanks, Aarav. Your StreamBox Premium autopay of nine hundred ninety-nine rupees didn't go through because the bank reported insufficient balance. Is the amount available now, and may I try the debit again?"),
            ("user", "Yeah yeah, there's money. Go ahead."),
            ("tool", ("retry_payment", {"customer_id": "cust_001", "customer_consented": True})),
            ("assistant", "I tried, but the bank declined it again for insufficient balance. Would you like me to schedule it for a later date, or send a link to pay from another account?"),
            ("user", "Oh. One second, let me move money from savings... done. Try now."),
            ("action", "add_funds"),
            ("tool", ("retry_payment", {"customer_id": "cust_001", "customer_consented": True})),
            ("assistant", "That worked. Nine hundred ninety-nine rupees is captured and your subscription continues without interruption. Thanks, Aarav. Goodbye!"),
        ],
        "outcome": "recovered", "sentiment": "neutral", "verified": True, "lang": "en",
        "summary": "Autopay failed for insufficient balance. First retry was declined despite the customer's claim; after he transferred funds the second retry captured INR 999. No follow-up needed.",
    },
    "cust_002": {
        "steps": [
            ("assistant", HELLO.format("Priya Sharma")),
            ("user", "Yes, that's me."),
            ("assistant", ASK.format(CARD4)),
            ("user", "7788."),
            ("tool", ("verify_identity", {"customer_id": "cust_002", "spoken_digits": "7788"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_002"})),
            ("assistant", "Thank you. Your StreamBox Max payment of two thousand four hundred ninety-nine rupees failed because the card linked to the mandate has expired, so a retry cannot work. I can text you a secure link to set up the new card. Shall I?"),
            ("user", "Oh right, I got a new card. Yes please send it."),
            ("tool", ("send_payment_link", {"customer_id": "cust_002", "purpose": "new_mandate"})),
            ("assistant", "Sent. Look for an SMS from StreamBox; the link is valid for 48 hours and sets up the new mandate too. I will never ask for your card number or OTP. Thanks, Priya. Goodbye!"),
        ],
        "outcome": "needs_new_mandate", "sentiment": "positive", "verified": True, "lang": "en",
        "summary": "Card on the mandate expired. A new-mandate payment link was sent by SMS; customer agreed to complete it. Follow up if unpaid in 48h.",
    },
    "cust_003": {   # says he resumed the mandate before actually doing it
        "steps": [
            ("assistant", HELLO.format("Rohan Verma")),
            ("user", "Haan bol raha hoon."),
            ("assistant", "Main aapke StreamBox subscription ke baare mein call kar rahi hoon. Ek quick check ke liye, UPI mandate se linked bank account ke last 4 digits bata denge?"),
            ("user", "एक शून्य तीन दो."),
            ("tool", ("verify_identity", {"customer_id": "cust_003", "spoken_digits": "एक शून्य तीन दो"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_003"})),
            ("assistant", "Thanks Rohan. Aapka StreamBox Family ka one thousand four hundred ninety-nine rupees ka autopay isliye fail hua kyunki mandate aapke UPI app mein paused hai. App mein Autopay, phir Manage, phir Resume kar dijiye, aur mujhe bata dijiye."),
            ("user", "Haan haan, kar diya."),
            ("tool", ("check_mandate_status", {"customer_id": "cust_003"})),
            ("assistant", "Bank ki taraf se abhi bhi paused dikh raha hai. Ek baar app mein Resume par tap karke confirm kar lenge?"),
            ("user", "Achha ruko... haan, ab resume ho gaya."),
            ("action", "resume_mandate"),
            ("tool", ("check_mandate_status", {"customer_id": "cust_003"})),
            ("assistant", "Ab active dikh raha hai. Kya main abhi payment try kar loon?"),
            ("user", "Haan kar do."),
            ("tool", ("retry_payment", {"customer_id": "cust_003", "customer_consented": True})),
            ("assistant", "Ho gaya, one thousand four hundred ninety-nine rupees capture ho gaye aur aapka plan chalu rahega. Thank you Rohan. Goodbye!"),
        ],
        "outcome": "recovered", "sentiment": "positive", "verified": True, "lang": "hinglish",
        "summary": "Mandate was paused in the UPI app. Customer first claimed it was resumed but the bank still showed paused; after he actually resumed it the status check passed and the retry captured INR 1,499.",
    },
    "cust_004": {
        "steps": [
            ("assistant", HELLO.format("Sneha Iyer")),
            ("user", "Yes. Did my payment fail? I have money in the account."),
            ("assistant", ASK.format(BANK4)),
            ("user", "5590."),
            ("tool", ("verify_identity", {"customer_id": "cust_004", "spoken_digits": "5590"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_004"})),
            ("assistant", "Thanks. Nothing was wrong on your side: the bank's servers were down when the nine hundred ninety-nine rupee debit ran. May I try it again now?"),
            ("user", "Sure."),
            ("tool", ("retry_payment", {"customer_id": "cust_004", "customer_consented": True})),
            ("assistant", "It went through, nine hundred ninety-nine rupees captured and your StreamBox plan continues. Sorry for the scare, and thanks. Goodbye!"),
        ],
        "outcome": "recovered", "sentiment": "positive", "verified": True, "lang": "en",
        "summary": "Bank-side outage caused the failure; retry with consent succeeded for INR 999.",
    },
    "cust_005": {
        "steps": [
            ("assistant", HELLO.format("Vikram Singh")),
            ("user", "Haan. Kya hua?"),
            ("assistant", "StreamBox subscription ke baare mein call hai. UPI mandate se linked account ke last 4 digits confirm kar denge?"),
            ("user", "Double eight four one."),
            ("tool", ("verify_identity", {"customer_id": "cust_005", "spoken_digits": "double eight four one"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_005"})),
            ("assistant", "Thanks. StreamBox Family Plus ka amount ab one thousand seven hundred ninety-nine rupees hai, jo aapke mandate ki limit se zyada hai, isliye retry nahi ho sakta. Main ek secure link bhej sakti hoon jisse naya mandate set ho jayega. Bhej doon?"),
            ("user", "Price badha di aur bata bhi nahi... theek hai bhej do."),
            ("tool", ("send_payment_link", {"customer_id": "cust_005", "purpose": "new_mandate"})),
            ("assistant", "Bhej diya, SMS check kar lijiye. Thank you Vikram. Goodbye!"),
        ],
        "outcome": "needs_new_mandate", "sentiment": "frustrated", "verified": True, "lang": "hinglish",
        "summary": "Plan amount exceeds the UPI mandate cap after a price increase. New-mandate link sent; customer unhappy about the price change. Follow up: feedback on price-change communication.",
    },
    "cust_007": {
        "steps": [
            ("assistant", HELLO.format("Karthik Nair")),
            ("user", "Yes. If this is about the StreamBox charge, I cancelled that in August."),
            ("assistant", "I'm sorry about that. So I can look at the right account, could you tell me the last 4 digits of the card on file?"),
            ("user", "3365. But I'm not paying anything."),
            ("tool", ("verify_identity", {"customer_id": "cust_007", "spoken_digits": "3365"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_007"})),
            ("user", "I want this escalated. I have the cancellation email."),
            ("tool", ("escalate_to_human", {"customer_id": "cust_007", "reason": "Customer says he cancelled StreamBox Max in August and has a cancellation email; disputes the charge."})),
            ("assistant", "Understood, I won't retry anything. I've opened a ticket and a support agent will contact you within one business day. Apologies for the trouble, Karthik. Goodbye."),
        ],
        "outcome": "escalated", "sentiment": "angry", "verified": True, "lang": "en",
        "summary": "Customer disputes the charge, claiming an August cancellation. No retry attempted; escalated to human support with the customer's statement.",
    },
    "cust_008": {
        "steps": [
            ("assistant", HELLO.format("Meera Joshi")),
            ("user", "Yes, hi."),
            ("assistant", ASK.format(BANK4)),
            ("user", "Nine one seven four."),
            ("tool", ("verify_identity", {"customer_id": "cust_008", "spoken_digits": "nine one seven four"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_008"})),
            ("assistant", "Thanks. Your StreamBox Family autopay of one thousand four hundred ninety-nine rupees failed for insufficient balance. Is the amount available now?"),
            ("user", "Not right now, my salary comes in a few days. Can you try then?"),
            ("tool", ("schedule_retry", {"customer_id": "cust_008", "retry_date": PROMISE_DATE, "note": "salary credited"})),
            ("assistant", f"Of course. I've scheduled the retry for {PROMISE_DATE} and your access stays active until then. Thanks, Meera. Goodbye!"),
        ],
        "outcome": "promise_to_pay", "sentiment": "neutral", "verified": True, "lang": "en", "promise": PROMISE_DATE,
        "summary": f"Insufficient balance; customer committed to funds on {PROMISE_DATE} and a retry was scheduled for that date. No debit was attempted on the call.",
    },
    "cust_009": {
        "steps": [
            ("assistant", HELLO.format("Arjun Kapoor")),
            ("user", "No, this is Rahul. I've had this number for a year, I don't know any Arjun."),
            ("tool", ("mark_wrong_number", {"customer_id": "cust_009", "person_denied_being_customer": True,
                                             "what_they_said": "No, this is Rahul, I don't know any Arjun."})),
            ("assistant", "I'm sorry for the disturbance, I'll make sure this number isn't called again. Have a good day. Goodbye."),
        ],
        "outcome": "wrong_number", "sentiment": "neutral", "verified": False, "lang": "en",
        "summary": "Number no longer belongs to the customer. No details disclosed; contact marked invalid.",
    },
    "cust_010": {
        "steps": [
            ("assistant", HELLO.format("Divya Pillai")),
            ("user", "Yes."),
            ("assistant", ASK.format(BANK4)),
            ("user", "4477."),
            ("tool", ("verify_identity", {"customer_id": "cust_010", "spoken_digits": "4477"})),
            ("tool", ("get_failure_details", {"customer_id": "cust_010"})),
            ("assistant", "Thanks. The nine hundred ninety-nine rupee StreamBox debit failed because the bank has a hold on that account, so a retry there cannot work. I can send a secure link so you can pay from any other account. Would that help?"),
            ("user", "Yes, that account is stuck in KYC. Send the link, I'll use my other one."),
            ("tool", ("send_payment_link", {"customer_id": "cust_010", "purpose": "one_time_payment"})),
            ("assistant", "Sent by SMS, valid 48 hours. Thanks for sorting it out, Divya. Goodbye!"),
        ],
        "outcome": "needs_new_mandate", "sentiment": "neutral", "verified": True, "lang": "en",
        "summary": "Linked account is under a bank hold. Payment link sent so the customer can pay from another account. Follow up if unpaid in 48h.",
    },
}


def call_obj(call_id: str, customer_id: str) -> dict[str, Any]:
    return {"id": call_id, "type": "webCall", "status": "in-progress",
            "assistantOverrides": {"variableValues": {"customer_id": customer_id}, "metadata": {"customer_id": customer_id}}}


def run_scenario(client: TestClient, customer_id: str, sc: dict[str, Any]) -> dict[str, Any]:
    call_id = f"dryrun-{customer_id}"
    call = call_obj(call_id, customer_id)
    client.post("/api/calls/register", json={"call_id": call_id, "customer_id": customer_id, "channel": "dry-run"})
    hook(client, {"type": "status-update", "status": "in-progress", "call": call})
    lines: list[str] = []
    for kind, payload in sc["steps"]:
        if kind == "tool":
            name, args = payload
            r = hook(client, {
                "type": "tool-calls", "call": call,
                "toolCallList": [{"id": f"tc_{name}_{len(lines)}", "name": name, "parameters": args}],
            })
            res = r.json()["results"][0]
            lines.append(f"Tool {name}: {res.get('result') or 'ERROR ' + str(res.get('error'))}")
        elif kind == "action":
            client.post(f"/api/customers/{customer_id}/customer-action", json={"action": payload})
            lines.append(f"[customer, on their own phone: {payload.replace('_', ' ')}]")
        else:
            hook(client, {"type": "transcript", "call": call, "role": kind, "transcriptType": "final", "transcript": payload})
            lines.append(f"{'AI' if kind == 'assistant' else 'User'}: {payload}")
    hook(client, {
        "type": "end-of-call-report", "call": call, "endedReason": "assistant-ended-call",
        "durationSeconds": 20 + 9 * len(sc["steps"]), "cost": 0.0,
        "artifact": {"transcript": "\n".join(lines)},
        "analysis": {
            "summary": sc["summary"],
            "structuredData": {
                "outcome": sc["outcome"], "promise_to_pay_date": sc.get("promise", ""),
                "customer_sentiment": sc["sentiment"], "identity_verified": sc["verified"],
                "customer_language": sc["lang"],
                "follow_up_required": sc["outcome"] in ("needs_new_mandate", "link_sent", "escalated"),
                "follow_up_note": "" if sc["outcome"] in ("recovered", "wrong_number", "promise_to_pay") else sc["summary"].split(". ")[-1],
            },
            "successEvaluation": "true",
        },
    })
    return db.get_customer(customer_id)["state"]


def main() -> int:
    if "--reset" in sys.argv:
        db.init(); db.reset_all()
    with TestClient(app) as client:
        print(f"{'customer':<10} {'name':<16} {'decision':<8} {'result':<20} {'debits':<7} detail")
        print("-" * 100)
        for c in db.list_customers():
            ok, why = policy.eligibility(c, ignore_window=True)
            if not ok:
                print(f"{c['id']:<10} {c['name']:<16} {'SKIP':<8} {'-':<20} {'-':<7} {why}")
                continue
            sc = SCENARIOS.get(c["id"])
            if not sc:
                print(f"{c['id']:<10} {c['name']:<16} {'SKIP':<8} {'-':<20} {'-':<7} no scenario")
                continue
            st = run_scenario(client, c["id"], sc)
            detail = st["payment_link"]["url"] if st.get("payment_link") else (
                st.get("scheduled_retry") or st.get("escalation") or st.get("payment_id") or "")
            print(f"{c['id']:<10} {c['name']:<16} {'CALLED':<8} {st['recovery_status']:<20} {st['debit_attempts']:<7} {detail}")
        customers = db.list_customers()
        recovered = sum(c["state"]["recovered_amount_inr"] for c in customers)
        total = sum(c["amount_inr"] for c in customers)

        def count(*statuses: str) -> int:
            return sum(1 for c in customers if c["state"]["recovery_status"] in statuses)

        print("-" * 100)
        print(f"Recovered now: INR {recovered} of INR {total} at risk; {count('promise_to_pay')} promise-to-pay, "
              f"{count('link_sent', 'needs_new_mandate')} links sent, {count('escalated')} escalated.")
        print("Open http://localhost:8000 to see the dashboard (uvicorn app.server:app).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
