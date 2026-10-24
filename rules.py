"""Deterministic chart checks. Hardcoded rules on purpose: explainable, fast, no training."""
import json
import os
from datetime import datetime

HANDOFF_TIME = os.getenv("HANDOFF_TIME", "19:00")  # shift change, HH:MM


def load_patients() -> dict:
    with open(os.path.join(os.path.dirname(__file__), "patients.json")) as f:
        return {p["room"]: p for p in json.load(f)}


def _t(hhmm: str):
    return datetime.strptime(hhmm, "%H:%M").time()


def chart_flags(p: dict) -> list[str]:
    flags = []
    v = p.get("vitals", [])
    if len(v) >= 3:
        sbp = [r["sbp"] for r in v[-3:]]
        if sbp[0] < sbp[1] < sbp[2] and sbp[2] >= 140:
            flags.append(f"Systolic BP rising 3 readings in a row: {sbp[0]} -> {sbp[1]} -> {sbp[2]}")
    if v:
        last = v[-1]
        if last["hr"] > 110:
            flags.append(f"Heart rate high at {last['hr']}")
        if last["spo2"] < 92:
            flags.append(f"Oxygen saturation low at {last['spo2']}%")
        if last["temp_c"] >= 38.0:
            flags.append(f"Fever {last['temp_c']} C")
    for m in p.get("meds", []):
        if not m["given"] and _t(m["due"]) <= _t(HANDOFF_TIME):
            flags.append(f"{m['name']} due {m['due']} not documented as given")
    return flags


if __name__ == "__main__":
    for room, p in load_patients().items():
        print(room, chart_flags(p))
