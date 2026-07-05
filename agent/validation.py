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
