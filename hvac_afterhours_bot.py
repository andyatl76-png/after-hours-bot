"""
After-Hours HVAC SMS Triage Bot
--------------------------------
Receives inbound SMS via Twilio webhook, uses the Anthropic API to
classify the issue, flags emergencies (especially gas smell), and
books non-emergency requests for the next business day.

SETUP
1. pip install -r requirements.txt   (see bottom of this file for contents)
2. Set environment variables:
     ANTHROPIC_API_KEY   - your Anthropic API key
     ON_CALL_PHONE       - phone number (E.164, e.g. +15551234567) to alert
                            for emergencies (used for logging / future paging)
     TWILIO_ACCOUNT_SID  - only needed if you enable outbound alert calls/texts
     TWILIO_AUTH_TOKEN   - only needed if you enable outbound alert calls/texts
     TWILIO_FROM_NUMBER  - your Twilio number, for outbound alerts
3. Run: python hvac_afterhours_bot.py
4. Point your Twilio phone number's "A MESSAGE COMES IN" webhook at:
     https://<your-host>/sms
   (use ngrok or similar for local testing)

This is a working starting point, not a production dispatch system —
swap the in-memory appointment store for a real DB/calendar, and wire
EMERGENCY_ALERT() to your actual on-call paging system (Twilio Voice
call, PagerDuty, Slack, etc.) before relying on it for real emergencies.
"""

import os
import json
import logging
from datetime import datetime, timedelta

from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hvac_bot")

app = Flask(__name__)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
ON_CALL_PHONE = os.environ.get("ON_CALL_PHONE", "")

# ---------------------------------------------------------------------------
# Very simple in-memory "appointment book". Replace with a real DB in
# production — this resets every time the process restarts.
# ---------------------------------------------------------------------------
BUSINESS_START_HOUR = 8
BUSINESS_END_HOUR = 17
SLOT_LENGTH_MIN = 60

appointments = {}  # {slot_iso_string: customer_phone}


def next_business_day(dt: datetime) -> datetime:
    """Return the next business day (Mon-Fri) at business start hour."""
    nxt = dt + timedelta(days=1)
    while nxt.weekday() >= 5:  # 5=Sat, 6=Sun
        nxt += timedelta(days=1)
    return nxt.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)


def find_next_available_slot() -> datetime:
    """Find the next open hourly slot on the next business day (or later)."""
    day = next_business_day(datetime.now())
    while True:
        slot = day
        while slot.hour < BUSINESS_END_HOUR:
            key = slot.isoformat()
            if key not in appointments:
                return slot
            slot += timedelta(minutes=SLOT_LENGTH_MIN)
        day = next_business_day(day)


def book_appointment(customer_phone: str) -> datetime:
    slot = find_next_available_slot()
    appointments[slot.isoformat()] = customer_phone
    return slot


# ---------------------------------------------------------------------------
# AI triage
# ---------------------------------------------------------------------------
TRIAGE_SYSTEM_PROMPT = """You are a triage assistant for an HVAC company's after-hours \
text line. A customer has texted in describing a problem. Classify the message and \
respond with ONLY a JSON object (no markdown, no commentary) in this exact shape:

{
  "is_emergency": true or false,
  "gas_smell": true or false,
  "issue_summary": "short plain-language summary of the reported problem",
  "reasoning": "one short sentence on why you classified it this way"
}

Rules for classification:
- gas_smell must be true if the customer mentions smelling gas, rotten eggs, sulfur,
  or a gas leak in any way. If gas_smell is true, is_emergency MUST also be true.
- Other emergencies (is_emergency = true) include: no heat in freezing/dangerous cold,
  no AC during extreme/dangerous heat (especially with elderly, infants, or medical
  conditions mentioned), active water leak/flooding from HVAC equipment, smoke, sparks,
  burning smell, or the customer explicitly saying it's an emergency or urgent safety issue.
- Routine issues (is_emergency = false) include: general no heat/no cooling without
  extreme conditions or safety mentions, strange noises, thermostat issues, maintenance
  requests, filter questions, minor comfort complaints.
- When uncertain, err toward flagging as an emergency — false positives are far safer
  than false negatives here.
"""


def triage_message(message_text: str) -> dict:
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=TRIAGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message_text}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        # Safety net: force emergency=true whenever gas_smell is true,
        # regardless of what the model returned.
        if result.get("gas_smell"):
            result["is_emergency"] = True

        return result
    except Exception:
        logger.exception("Triage classification failed; defaulting to emergency for safety")
        # Fail safe: if the AI call or parsing breaks, treat it as an
        # emergency so a real human follows up rather than nothing happening.
        return {
            "is_emergency": True,
            "gas_smell": False,
            "issue_summary": message_text[:200],
            "reasoning": "Classification failed; defaulting to emergency out of caution.",
        }


# ---------------------------------------------------------------------------
# Emergency alerting (stub — wire this up to real paging)
# ---------------------------------------------------------------------------
def emergency_alert(customer_phone: str, triage: dict):
    """
    Placeholder for real on-call paging. Right now this just logs loudly.
    Swap in a Twilio Voice call, PagerDuty trigger, Slack webhook, etc.
    """
    logger.warning(
        "EMERGENCY ALERT | customer=%s | gas_smell=%s | summary=%s | notify=%s",
        customer_phone,
        triage.get("gas_smell"),
        triage.get("issue_summary"),
        ON_CALL_PHONE or "(ON_CALL_PHONE not set)",
    )


# ---------------------------------------------------------------------------
# Flask route
# ---------------------------------------------------------------------------
@app.route("/sms", methods=["POST"])
def sms_webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "")

    logger.info("Inbound SMS from %s: %s", from_number, incoming_msg)

    triage = triage_message(incoming_msg)
    resp = MessagingResponse()

    if triage.get("gas_smell"):
        emergency_alert(from_number, triage)
        reply = (
            "This may be a GAS LEAK emergency. If you smell gas: leave the "
            "building immediately, do not use any switches or open flames, "
            "and call your gas utility or 911 from outside. Our on-call "
            "technician has been alerted and will follow up right away."
        )
    elif triage.get("is_emergency"):
        emergency_alert(from_number, triage)
        reply = (
            "This sounds urgent, so we've flagged it as an emergency and "
            "alerted our on-call technician now. They'll reach out to you "
            "shortly. If conditions worsen or feel unsafe, call 911."
        )
    else:
        slot = book_appointment(from_number)
        pretty_time = slot.strftime("%A, %B %d at %I:%M %p")
        reply = (
            f"Thanks for the details — got it: \"{triage.get('issue_summary')}\". "
            f"This isn't an emergency, so we've booked you for {pretty_time}. "
            "Reply if that doesn't work and we'll find another time."
        )

    resp.message(reply)
    return Response(str(resp), mimetype="application/xml")


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(port=5000, debug=True)


# ---------------------------------------------------------------------------
# requirements.txt (create this as a separate file):
#
# flask
# twilio
# anthropic
# ---------------------------------------------------------------------------
