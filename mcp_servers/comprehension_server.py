"""Comprehension MCP server — the AI CORE of the project.

This is the one place the task is genuinely NOT rule-based: understanding an
arbitrary, never-before-seen timetable. The VLM:
  - locates the prayer-time grid amid surrounding graphics,
  - infers the labeling scheme (Begins/Jamaat vs Athan/Iqamah vs Start/Congregation),
  - maps source columns onto the canonical schema,
  - resolves the Maghrib single-vs-double-column rule,
  - extracts EVERY date row visible (a full month, not just "today"),
  - and returns PER-COLUMN CONFIDENCE + a rationale.

validate_extraction is deterministic (consistency checks). Its job is to give the
agent something concrete to reason against so it can self-correct.
"""
import json
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from openai import OpenAI

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import settings  # noqa: E402
from agent.validation import (  # noqa: E402
    validate_json, compute_maghrib_iqamah, merge_extractions as _merge_extractions)

mcp = FastMCP("comprehension")
client = OpenAI(api_key=settings.openai_api_key)

SYSTEM = """You read Islamic prayer timetables from arbitrary sources and map them
onto a fixed canonical schema. Sources are NOT standardized: labels vary
("Begins"/"Start"/"Athan" all mean the Salah/start time; "Jamaat"/"Iqamah"/
"Congregation" all mean the Iqamah time), column order varies, and the grid may be
surrounded by logos or other graphics you must ignore.

The source is normally a CALENDAR of many dates (e.g. a whole month), not a single
day. Extract EVERY date row visible in what you were given, in chronological order
— do not collapse to just "today", and do not invent dates that aren't shown.

Canonical per-date columns:
- Salah, in order: Fajr, Sunrise, Dhuhr, Asr, Maghrib, Isha
- Iqamah, in order: Fajr, Dhuhr, Asr, Maghrib, Isha

Special Maghrib rule for the IQAMAH side: output the RAW CLOCK TIME of that
date's Maghrib jamaat (Iqamah), same format as every other time — e.g. "9:28 PM".
Do NOT compute a minutes-difference yourself; a deterministic step afterward
subtracts it from the Salah (begin) time, since that arithmetic is easy for code
to get right and easy for you to get subtly wrong across many rows. If Maghrib
has only ONE column in the source (no separate jamaat time), leave this field
EMPTY — the deterministic step defaults that to "1".

Sources are often INCONSISTENT about how many columns each prayer gets — e.g.
Fajr/Dhuhr/Asr/Isha may each show two sub-columns (Begin, Jama'ah) while
Maghrib shows only ONE. Do not assume every prayer has the same column count.
If you find yourself about to fill Maghrib's jamaat with a value that looks
like it belongs to the NEXT prayer (Isha) rather than something genuinely
under a Maghrib heading, that is a sign Maghrib is single-column here — leave
it EMPTY rather than borrowing a neighboring column's value. In real practice
Maghrib jamaat is almost always within a few minutes of its begin time (the
prayer window is narrow); a value that implies a gap of 20+ minutes is far
more likely a misread than a real timing.

Jumuah (Friday prayer) is DIFFERENT: it is normally one fixed weekly time (or two,
if there are two sessions), not something that changes by date. Report it ONCE,
not per-date, in a separate "jumuah" object: {"Jumuah 1": str, "Jumuah 2": str}.

For every COLUMN (not per-cell) give a confidence in [0,1] reflecting how sure you
are about that column as a whole — the label mapping, the value format, and the
readability across the dates you extracted. Be honest: if a column SHOULD exist
but is unreadable, ambiguous, or you had to guess the mapping, lower its
confidence. It is better to report low confidence than to be confidently wrong.

Some columns are legitimately ABSENT from a given source, not merely hard to read —
e.g. many masjids hold only one Jumuah service, so "Jumuah 2" has no value at all.
Mark a column not_applicable:true (with confidence 1.0) ONLY when the source
itself POSITIVELY PROVES it does not exist — e.g. a Jumuah list that is
structurally singular (only one slot exists at all, so a second cannot exist), or
explicit text saying so. This is a high bar: you must be able to point to specific
evidence of absence, not merely that you didn't see it.

If a column simply never appears anywhere in what you were given — no matching
row, column, or mention — that is UNKNOWN, not not_applicable. The masjid may well
hold that prayer/service, just not listed in this particular source excerpt. For
UNKNOWN columns, keep not_applicable:false, leave every date's value for it empty
(or omit "jumuah" entries), and give a LOW column confidence (e.g. <=0.3) — do not
use the not_applicable escape hatch just because a column was absent from the
source you were shown.

Return ONLY JSON, no prose, matching exactly:
{
 "masjid_name": str,
 "detected_label_scheme": str,
 "maghrib_single_column": bool,
 "rationale": str,
 "overall_confidence": float,
 "column_confidence": {
   "salah": {"Fajr":float,"Sunrise":float,"Dhuhr":float,"Asr":float,"Maghrib":float,"Isha":float},
   "iqamah": {"Fajr":float,"Dhuhr":float,"Asr":float,"Maghrib":float,"Isha":float,"Jumuah 1":float,"Jumuah 2":float}
 },
 "not_applicable": {"salah": {}, "iqamah": {"Jumuah 1": bool, "Jumuah 2": bool}},
 "jumuah": {"Jumuah 1": str, "Jumuah 2": str},
 "rows": [
   {"date": "YYYY-MM-DD",
    "salah":  {"Fajr":str,"Sunrise":str,"Dhuhr":str,"Asr":str,"Maghrib":str,"Isha":str},
    "iqamah": {"Fajr":str,"Dhuhr":str,"Asr":str,"Maghrib":str,"Isha":str}},
   ... one object per date found in the source, chronological
 ]
}
"""


def _call(content_blocks) -> dict:
    resp = client.chat.completions.create(
        model=settings.vlm_model,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": content_blocks}],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=8192,
    )
    return json.loads(resp.choices[0].message.content)


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _apply_grounding(extraction: dict, source_text: str, previous: dict | None = None) -> dict:
    """Deterministic safety net: the VLM's own stated confidence can itself be a
    hallucination — it can confidently claim a value is 'in the text' when it
    plainly is not (observed: fabricated Jumuah times attributed to text that
    didn't mention Jumuah at all). If we were given source_text, verify each
    non-empty, non-N/A value literally appears in it; if not, cap that COLUMN's
    confidence regardless of what the model claimed. This can't catch every
    hallucination (a fabricated value that happens to coincide with a real value
    for a DIFFERENT field elsewhere in the source will still slip through), but it
    catches outright inventions.

    `previous` (the prior round's extraction, for recheck calls) lets us skip
    values the model merely carried forward unchanged — a recheck's source_text
    is often a narrow, targeted excerpt (e.g. just a Jumuah notice), and values
    untouched by this round were already vetted against their OWN, earlier
    source_text; re-checking them against this narrower excerpt would produce
    false positives."""
    if not source_text:
        return extraction  # can't substring-check against an image; trust the VLM
    norm_source = _normalize(source_text)
    not_applicable = extraction.get("not_applicable", {})
    ungrounded_cols = set()

    prev_rows_by_date = {r.get("date"): r for r in (previous or {}).get("rows", [])} if previous else {}
    for row in extraction.get("rows", []):
        prev_row = prev_rows_by_date.get(row.get("date"), {})
        for side in ("salah", "iqamah"):
            na_side = not_applicable.get(side, {})
            for field, value in row.get(side, {}).items():
                if na_side.get(field):
                    continue
                value = str(value or "").strip()
                if not value:
                    continue
                if previous is not None:
                    prev_value = str(prev_row.get(side, {}).get(field, "")).strip()
                    if prev_value == value:
                        continue  # unchanged from the prior round; already vetted
                if _normalize(value) not in norm_source:
                    ungrounded_cols.add(f"{side}.{field}")

    prev_jumuah = (previous or {}).get("jumuah", {}) if previous else {}
    for field, value in extraction.get("jumuah", {}).items():
        if not_applicable.get("iqamah", {}).get(field):
            continue
        value = str(value or "").strip()
        if not value:
            continue
        if previous is not None and str(prev_jumuah.get(field, "")).strip() == value:
            continue
        if _normalize(value) not in norm_source:
            ungrounded_cols.add(f"iqamah.{field}")

    if ungrounded_cols:
        cc = extraction.setdefault("column_confidence", {"salah": {}, "iqamah": {}})
        for col in ungrounded_cols:
            side, field = col.split(".", 1)
            cc.setdefault(side, {})
            cc[side][field] = min(cc[side].get(field, 0.0), 0.15)
        extraction["rationale"] = (
            extraction.get("rationale", "") +
            f" [GROUNDING CHECK FAILED — at least one value in these columns does "
            f"not literally appear in the source text given, confidence capped; "
            f"verify manually: {', '.join(sorted(ungrounded_cols))}.]")
    return extraction


@mcp.tool()
def vlm_read_timetable(masjid_name: str, source_text: str = "",
                       image_base64: str = "") -> dict:
    """Read a timetable (normally a full month calendar) from text (HTML/PDF text)
    and/or an image, and return the canonical mapping with per-column confidence.
    This is the agent's core AI tool."""
    blocks = [{"type": "text",
               "text": f"Masjid: {masjid_name}\n\nSource content follows."}]
    if source_text:
        blocks.append({"type": "text", "text": source_text[:24000]})
    if image_base64:
        blocks.append({"type": "image_url",
                       "image_url": {"url": f"data:image/png;base64,{image_base64}"}})
    try:
        extraction = _apply_grounding(_call(blocks), source_text)
        extraction = compute_maghrib_iqamah(extraction)
        return {"ok": True, "extraction": extraction}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def vlm_recheck_field(masjid_name: str, contradiction: str,
                      previous_json: str, source_text: str = "",
                      image_base64: str = "") -> dict:
    """Self-correction tool. Given a specific contradiction the validator found,
    re-examine the source and return a corrected extraction (same JSON schema)."""
    blocks = [{"type": "text", "text":
               f"Masjid: {masjid_name}\nYour previous extraction was:\n{previous_json}\n\n"
               f"A validator found this contradiction: {contradiction}\n"
               f"Re-examine the source carefully and return a corrected extraction."}]
    if source_text:
        blocks.append({"type": "text", "text": source_text[:24000]})
    if image_base64:
        blocks.append({"type": "image_url",
                       "image_url": {"url": f"data:image/png;base64,{image_base64}"}})
    try:
        try:
            previous = json.loads(previous_json)
        except Exception:
            previous = None
        extraction = _apply_grounding(_call(blocks), source_text, previous)
        extraction = compute_maghrib_iqamah(extraction)
        return {"ok": True, "extraction": extraction}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def merge_extractions(extraction_jsons: list[str]) -> dict:
    """Combine multiple partial extractions (one JSON string per call to
    vlm_read_timetable) into a single extraction. Use this after processing a
    source in chunks (via acquisition's chunk_timetable_by_month, for a
    source spanning much more than one month) — call vlm_read_timetable once
    per chunk, collect each result's extraction JSON, then pass all of them
    here to get back the one combined extraction to validate and publish.
    Rows are concatenated and sorted chronologically; per-column confidence
    takes the minimum across chunks (conservative)."""
    try:
        extractions = [json.loads(s) for s in extraction_jsons]
        return {"ok": True, "extraction": _merge_extractions(extractions)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def validate_extraction(extraction_json: str) -> dict:
    """Deterministic consistency checks. Returns a list of contradictions the agent
    can reason about. Empty list => internally consistent. Logic lives in
    agent/validation.py so it is testable without MCP."""
    return validate_json(extraction_json)


if __name__ == "__main__":
    mcp.run()
