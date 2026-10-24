"""
Voice Shift Handoff — c0mpiled-16 (No App track)

Outgoing nurse calls the Twilio number, keys in each room number, speaks the handoff.
Claude merges the spoken notes with chart data, builds an SBAR handoff, flags anomalies
and discrepancies, then CALLS the incoming nurse and reads it aloud (SMS copy optional).
No screen anywhere in the loop.
"""
import json
import os
import time
import uuid

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from twilio.rest import Client
from twilio.twiml.voice_response import Gather, VoiceResponse

from rules import chart_flags, load_patients

load_dotenv()

app = FastAPI()
tw = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
llm = Anthropic()  # reads ANTHROPIC_API_KEY

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
TWILIO_NUMBER = os.environ["TWILIO_NUMBER"]
INCOMING_NURSE = os.environ["INCOMING_NURSE_NUMBER"]
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
SEND_SMS = os.getenv("SEND_SMS", "false").lower() == "true"
VOICE = os.getenv("TTS_VOICE", "Polly.Joanna-Neural")  # natural voice; set TTS_VOICE=alice to fall back

PATIENTS = load_patients()
SESSIONS: dict[str, dict] = {}  # CallSid -> {"notes": {room: text}, "room": str}
REPORTS: dict[str, dict] = {}   # report_id -> report JSON

MED_HINTS = ("metoprolol, lisinopril, vancomycin, heparin, insulin, PRN, SpO2, "
             "blood pressure, sats, telemetry, ambulating, NPO, foley, IV, pain score")


def twiml(vr: VoiceResponse) -> Response:
    return Response(content=str(vr), media_type="application/xml")


def ask_room(vr: VoiceResponse, prompt: str) -> VoiceResponse:
    g = Gather(input="dtmf", finish_on_key="#", timeout=10, action="/room", method="POST")
    g.say(prompt, voice=VOICE)
    vr.append(g)
    vr.redirect("/room", method="POST")  # no input -> /room handles it
    return vr


# ---------- Inbound: outgoing nurse gives handoff ----------

@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    SESSIONS[form["CallSid"]] = {"notes": {}, "room": None}
    vr = VoiceResponse()
    return twiml(ask_room(vr, "Handoff line. Room number, then pound."))


@app.post("/room")
async def room(request: Request, background: BackgroundTasks):
    form = await request.form()
    sid = form["CallSid"]
    session = SESSIONS.setdefault(sid, {"notes": {}, "room": None})
    digits = (form.get("Digits") or "").strip()
    vr = VoiceResponse()

    if not digits:
        return twiml(ask_room(vr, "Room number, then pound. Zero pound to finish."))

    if digits == "0":
        if not session["notes"]:
            return twiml(ask_room(vr, "No patients yet. Room number, then pound."))
        report_id = uuid.uuid4().hex[:8]
        background.add_task(build_and_deliver, report_id, dict(session["notes"]))
        vr.say(f"Got {len(session['notes'])} patients. Sending the handoff now.", voice=VOICE)
        vr.hangup()
        return twiml(vr)

    session["room"] = digits
    known = "" if digits in PATIENTS else " I don't have a chart for that room, but I'll record it."
    g = Gather(input="speech", action="/notes", method="POST", speech_timeout="3",
               timeout=8, enhanced=True, speech_model="phone_call", hints=MED_HINTS)
    g.say(f"Room {' '.join(digits)}.{known} Go ahead.", voice=VOICE)
    vr.append(g)
    vr.redirect("/notes", method="POST")
    return twiml(vr)


@app.post("/notes")
async def notes(request: Request):
    form = await request.form()
    session = SESSIONS.setdefault(form["CallSid"], {"notes": {}, "room": None})
    text = (form.get("SpeechResult") or "").strip()
    room_no = session.get("room")
    if room_no and text:
        prev = session["notes"].get(room_no, "")
        session["notes"][room_no] = (prev + " " + text).strip()
    vr = VoiceResponse()
    msg = "Got it." if text else "I didn't catch that."
    return twiml(ask_room(vr, f"{msg} Next room, or zero pound to finish."))


# ---------- Brain: Claude builds the SBAR handoff ----------

SYSTEM = """You are a charge-nurse assistant that turns a spoken shift handoff into a safe, structured handoff
for the incoming nurse. You receive, per room: the outgoing nurse's spoken notes (speech-to-text, may contain
transcription errors) and automated chart flags computed from vitals and the medication record.

Rules:
- Never invent clinical facts. Use only the notes and chart flags.
- Call out DISCREPANCIES explicitly: where the spoken notes conflict with chart flags
  (e.g. nurse says "stable" but blood pressure is trending up, or a med the chart shows as missed).
- Order patients by urgency: discrepancies and active flags first.
- Use SBAR (Situation, Background, Assessment, Recommendation), one line each, in the patients array.
- Recommendations stay within nursing scope: recheck, reassess, verify with pharmacy, notify the provider.
  Never recommend specific drugs or doses.

The spoken_summary is read aloud by text-to-speech on a phone call to a busy nurse. It must:
- Be under 110 words in total (about 45 seconds aloud).
- Open with one sentence saying how many patients need attention, e.g. "Two of your three patients need attention."
- For each flagged patient, LEAD with the discrepancy in the pattern: "Room twelve. Reported stable, but ..."
  then one short action. Two sentences maximum per flagged patient.
- Give each patient with no flags one short sentence at the end.
- Use plain spoken words: say numbers as digits ("152"), rooms as words ("room twelve"), no abbreviations
  like BP, IV, PO or CHF, no SBAR labels, no lists, no parentheses.

Example of the spoken style (illustrative only, never copy its facts):
"Two of your three patients need attention. Room twelve. Reported stable, but blood pressure has risen three
readings in a row, now 152. Recheck vitals and notify the provider. Room fourteen. Antibiotics were reported
on schedule, but the 6 PM vancomycin is not documented and there is a new fever of 38.3. Verify the dose
with pharmacy. Room seventeen is stable, walking, and on clear liquids."

Return ONLY JSON, no markdown, with this shape:
{"patients":[{"room":"12","priority":"high|medium|low","situation":"","background":"","assessment":"",
"recommendation":"","discrepancies":[""],"flags":[""]}],
"spoken_summary":"full script to read aloud, most urgent first",
"sms_summary":"under 300 characters"}"""


def build_report(notes: dict[str, str]) -> dict:
    payload = []
    for room_no, text in notes.items():
        p = PATIENTS.get(room_no)
        payload.append({
            "room": room_no,
            "spoken_notes": text,
            "patient": {"name": p["name"], "age": p["age"], "dx": p["dx"]} if p else None,
            "chart_flags": chart_flags(p) if p else ["No chart on file"],
        })
    msg = llm.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)


def build_and_deliver(report_id: str, notes: dict[str, str]) -> None:
    started = time.time()
    try:
        report = build_report(notes)
    except Exception as e:  # never leave the incoming nurse with nothing
        print("LLM failed:", e)
        report = {"spoken_summary": "Automatic summary failed. Raw notes follow. " +
                  " ".join(f"Room {r}: {t}." for r, t in notes.items()),
                  "sms_summary": "Handoff summary failed; check raw notes.", "patients": []}
    report["notes"] = notes
    REPORTS[report_id] = report
    print(json.dumps(report, indent=2))

    tw.calls.create(to=INCOMING_NURSE, from_=TWILIO_NUMBER, url=f"{PUBLIC_URL}/deliver/{report_id}")
    words = len(report.get("spoken_summary", "").split())
    print(f"\n>>> Report {report_id}: built and callback placed in {time.time() - started:.1f}s, "
          f"{words} spoken words (~{words / 2.5:.0f}s aloud)\n")
    if SEND_SMS:
        try:
            tw.messages.create(to=INCOMING_NURSE, from_=TWILIO_NUMBER,
                               body=report.get("sms_summary", "")[:320])
        except Exception as e:
            print("SMS failed (10DLC/trial limits are common):", e)


# ---------- Outbound: incoming nurse hears the handoff ----------

@app.post("/deliver/{report_id}")
async def deliver(report_id: str):
    vr = VoiceResponse()
    report = REPORTS.get(report_id)
    if not report:
        vr.say("Sorry, that handoff could not be found.", voice=VOICE)
        return twiml(vr)
    vr.say("Your shift handoff.", voice=VOICE)
    vr.say(report["spoken_summary"], voice=VOICE)
    g = Gather(input="dtmf", num_digits=1, timeout=6, action=f"/deliver/{report_id}", method="POST")
    g.say("Press any key to replay.", voice=VOICE)
    vr.append(g)
    return twiml(vr)


# ---------- Demo helpers ----------

@app.post("/demo/trigger")
async def demo_trigger(background: BackgroundTasks):
    """Backup: run the canned demo handoff without speaking (if speech recognition flakes on stage)."""
    with open(os.path.join(os.path.dirname(__file__), "demo_notes.json")) as f:
        notes = json.load(f)
    report_id = uuid.uuid4().hex[:8]
    background.add_task(build_and_deliver, report_id, notes)
    return {"report_id": report_id, "status": "calling incoming nurse"}


@app.get("/report/{report_id}")
async def get_report(report_id: str):
    return JSONResponse(REPORTS.get(report_id, {"error": "not found"}))


@app.get("/reports")
async def list_reports():
    return list(REPORTS.keys())
