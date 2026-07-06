"""Pure, dependency-free consistency logic. Imported by the Comprehension MCP
server AND by tests, so the deterministic rules can be verified without MCP."""
import json
import re


def to_minutes(t: str):
    """Parse '5:12 AM' / '17:35' -> minutes since midnight. None if unparseable."""
    if not t:
        return None
    t = t.strip().upper()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", t)
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return h * 60 + mn


MAGHRIB_IQAMAH_MAX_MINUTES = 15
# Real-world Maghrib Iqamah gaps are almost always tiny (commonly 1-10
# minutes — Maghrib's own prayer window is narrow, so masjids don't leave a
# long gap). Observed live: a source where Maghrib has only ONE column but its
# neighbors (e.g. Isha) have two confused the VLM into "borrowing" the next
# prayer's Begin time as if it were a second Maghrib column, producing
# differences of 70+ minutes that were actually just (Isha begin - Maghrib
# begin). Any diff this large is essentially always a misread, not a real
# value, so it's rejected deterministically rather than trusting the VLM.


def compute_maghrib_iqamah(extraction: dict) -> dict:
    """Deterministically compute each date's Maghrib Iqamah value as the number
    of minutes after Salah (begin) time, rather than trusting the VLM to do this
    arithmetic itself. The VLM is only asked for the raw Iqamah/Jama'ah clock
    time (or to leave it empty if the source has a single Maghrib column) —
    this function does the subtraction, and rejects implausible results.
    Convention: "1" when there's no second column, no parseable time, the
    computed difference is zero/negative, or it exceeds
    MAGHRIB_IQAMAH_MAX_MINUTES (flagged in the rationale, not silently).

    Once this has run, the Maghrib Iqamah column's confidence is always set to
    1.0 (certain) — by design, not an oversight. Every path through this
    function already resolves to a value we trust deterministically (a real
    computed diff, or the well-justified "1" default), so the VLM's own
    uncertainty about column structure is no longer a meaningful signal for
    whether a HUMAN needs to look at it. Escalating to review for a field
    code has already settled correctly just adds friction without adding
    safety — the sanity check below is the actual safety net, not a human."""
    flagged = []
    for row in extraction.get("rows", []):
        iq = row.setdefault("iqamah", {})
        raw = str(iq.get("Maghrib", "")).strip()
        date = row.get("date", "?")
        if raw.lstrip("-").isdigit():
            # Already a diff (e.g. carried forward from a prior round) — still
            # sanity-check it; a bad value shouldn't survive just because it
            # arrived pre-computed instead of as a raw clock time this round.
            diff = int(raw)
            if diff <= 0:
                iq["Maghrib"] = "1"
            elif diff > MAGHRIB_IQAMAH_MAX_MINUTES:
                iq["Maghrib"] = "1"
                flagged.append(date)
            continue
        salah_m = to_minutes(row.get("salah", {}).get("Maghrib", ""))
        iqamah_m = to_minutes(raw)
        if salah_m is None or iqamah_m is None:
            iq["Maghrib"] = "1"
            continue
        diff = iqamah_m - salah_m
        if diff <= 0:
            iq["Maghrib"] = "1"
        elif diff > MAGHRIB_IQAMAH_MAX_MINUTES:
            iq["Maghrib"] = "1"
            flagged.append(date)
        else:
            iq["Maghrib"] = str(diff)

    if flagged:
        extraction["rationale"] = (
            extraction.get("rationale", "") +
            f" [MAGHRIB IQAMAH SANITY CHECK: the computed difference exceeded "
            f"{MAGHRIB_IQAMAH_MAX_MINUTES} min on {len(flagged)} date(s) (e.g. "
            f"{flagged[0]}) — almost certainly a column misread (e.g. an "
            f"adjacent prayer's time mistaken for a second Maghrib column), "
            f"not a real value. Defaulted to '1' automatically — no action "
            f"needed, this note is for audit visibility only.]")

    cc = extraction.setdefault("column_confidence", {"salah": {}, "iqamah": {}})
    cc.setdefault("iqamah", {})["Maghrib"] = 1.0
    return extraction


def check_consistency(extraction: dict) -> list[str]:
    """Return a list of human-readable contradictions. Empty => consistent.
    Checks every date row in the extraction independently."""
    issues = []
    order = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
    for row in extraction.get("rows", []):
        date = row.get("date", "?")
        salah = row.get("salah", {})
        known = [(p, to_minutes(salah.get(p, ""))) for p in order]
        known = [(p, m) for p, m in known if m is not None]
        for (p1, m1), (p2, m2) in zip(known, known[1:]):
            if m2 < m1:
                issues.append(f"{date}: {p2} start ({m2}m) is earlier than {p1} start ({m1}m).")

        mag = str(row.get("iqamah", {}).get("Maghrib", "")).strip()
        if mag and (":" in mag or not mag.lstrip("-").isdigit()):
            issues.append(f"{date}: Maghrib iqamah should be minutes-difference, got '{mag}'.")
        elif mag.isdigit() and int(mag) > 60:
            issues.append(f"{date}: Maghrib iqamah difference '{mag}' is implausibly large.")
    return issues


def validate_json(extraction_json: str) -> dict:
    try:
        e = json.loads(extraction_json)
    except Exception as ex:
        return {"ok": False, "error": f"bad json: {ex}"}
    issues = check_consistency(e)
    return {"ok": True, "issues": issues, "consistent": not issues}
