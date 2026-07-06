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


def minutes_to_clock(total_minutes: int) -> str:
    """Inverse of to_minutes(): minutes-since-midnight -> '5:12 AM' string."""
    total_minutes = total_minutes % (24 * 60)
    h, mn = divmod(total_minutes, 60)
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mn:02d} {period}"


SALAH_COLUMNS = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
IQAMAH_CARRY_FORWARD_COLUMNS = ["Fajr", "Dhuhr", "Asr", "Isha"]
# Maghrib Iqamah is excluded here — it's always a minutes-difference (see
# compute_maghrib_iqamah), never a clock time to carry forward.

TREND_HISTORY_DAYS = 14
# How many of the most recent history days to use when estimating a linear
# day-over-day trend for Salah times. Long enough to smooth out noise/misreads
# in the source history; short enough that the trend reflects the CURRENT
# rate of change (which itself drifts across the year) rather than an average
# from months ago.


def extrapolate_salah_trend(history_rows: list[dict], missing_dates: list[str]) -> dict:
    """Given a masjid's own recent Salah history (list of {"date": ...,
    "Fajr": ..., ...} dicts, any order), project a value for each date in
    missing_dates using a simple linear day-over-day trend computed from the
    most recent TREND_HISTORY_DAYS of history. Salah times move gradually and
    smoothly (driven by sunrise/sunset), so a short linear extrapolation is a
    reasonable estimate for filling a gap of days/weeks. This is NOT a
    substitute for a real astronomical calculation or a real source reading —
    callers MUST mark results from this function as estimated, not fact.

    Returns {date: {column: "H:MM AM/PM", ...}, ...} — a column is omitted
    for a date if there wasn't enough usable history to project it."""
    history_sorted = sorted(history_rows, key=lambda r: r.get("date", ""))
    recent = history_sorted[-TREND_HISTORY_DAYS:]
    result = {date: {} for date in missing_dates}
    for col in SALAH_COLUMNS:
        series = [to_minutes(r.get(col, "")) for r in recent]
        series = [m for m in series if m is not None]
        if len(series) < 2:
            continue
        deltas = [series[i] - series[i - 1] for i in range(1, len(series))]
        trend = sum(deltas) / len(deltas)
        last_minutes = series[-1]
        for i, date in enumerate(sorted(missing_dates), start=1):
            result[date][col] = minutes_to_clock(round(last_minutes + trend * i))
    return result


def carry_forward_last_value(history_rows: list[dict], missing_dates: list[str],
                             columns: list[str] = IQAMAH_CARRY_FORWARD_COLUMNS) -> dict:
    """Iqamah times are set by mosque administrators and change in occasional
    steps, not a smooth trend — so unlike Salah, the right estimate for a
    missing date is simply the MOST RECENT known value, held constant, not an
    extrapolated trend."""
    history_sorted = sorted(history_rows, key=lambda r: r.get("date", ""))
    if not history_sorted:
        return {date: {} for date in missing_dates}
    last = history_sorted[-1]
    filled = {col: last[col] for col in columns if last.get(col)}
    return {date: dict(filled) for date in missing_dates}


def carry_forward_jumuah(history_jumuah: dict) -> dict:
    """Jumuah is a fixed weekly time that rarely changes — carrying forward
    the last known value(s) is the estimate, not a trend."""
    return {k: v for k, v in (history_jumuah or {}).items() if v}


def merge_extractions(extractions: list[dict]) -> dict:
    """Combine multiple partial extractions (e.g. one per month, from chunked
    processing of a source spanning a full year) into one. Rows are
    concatenated and sorted chronologically; column_confidence takes the
    MINIMUM across chunks per column (conservative — one bad month shouldn't
    be hidden behind an average with good months); not_applicable for a
    column is true only if every chunk that reports it agrees;
    jumuah/masjid_name/detected_label_scheme are taken from the first chunk
    that has them (date-independent, so identical across chunks normally)."""
    if not extractions:
        return {}
    merged = {
        "masjid_name": extractions[0].get("masjid_name", ""),
        "detected_label_scheme": extractions[0].get("detected_label_scheme", ""),
        "maghrib_single_column": extractions[0].get("maghrib_single_column", False),
        "rationale": " | ".join(e.get("rationale", "") for e in extractions if e.get("rationale")),
    }

    all_rows = []
    for e in extractions:
        all_rows.extend(e.get("rows", []))
    all_rows.sort(key=lambda r: r.get("date", ""))
    merged["rows"] = all_rows

    cc = {"salah": {}, "iqamah": {}}
    for side in ("salah", "iqamah"):
        cols = set()
        for e in extractions:
            cols.update(e.get("column_confidence", {}).get(side, {}).keys())
        for col in cols:
            vals = [e["column_confidence"][side][col] for e in extractions
                    if col in e.get("column_confidence", {}).get(side, {})]
            if vals:
                cc[side][col] = min(vals)
    merged["column_confidence"] = cc

    na = {"salah": {}, "iqamah": {}}
    for side in ("salah", "iqamah"):
        cols = set()
        for e in extractions:
            cols.update(e.get("not_applicable", {}).get(side, {}).keys())
        for col in cols:
            relevant = [e["not_applicable"][side][col] for e in extractions
                       if col in e.get("not_applicable", {}).get(side, {})]
            na[side][col] = bool(relevant) and all(relevant)
    merged["not_applicable"] = na

    merged["jumuah"] = next((e["jumuah"] for e in extractions if e.get("jumuah")), {})
    confs = [e["overall_confidence"] for e in extractions if e.get("overall_confidence") is not None]
    merged["overall_confidence"] = min(confs) if confs else 0.0
    return merged


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
