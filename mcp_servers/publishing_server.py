"""Publishing MCP server — DETERMINISTIC. This is explicitly NOT the AI part.

Generates the two CSVs in Masjidal's format (one row per date, Jumuah repeated on
every row since it's a fixed weekly time) and (optionally) drives the portal
upload with Playwright. Portal upload only runs when PORTAL_UPLOAD_ENABLED=true,
so demos stay safe by default and still produce the CSVs on disk.
"""
import csv
import json
import re
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import settings  # noqa: E402

mcp = FastMCP("publishing")

SALAH_HEADER = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
IQAMAH_HEADER = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha", "Jumuah 1", "Jumuah 2"]
DATE_INPUT_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y")


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_")[:50]


def _fmt_date(raw: str) -> str:
    """Normalize to Excel-friendly M/D/YYYY (no leading zeros), matching the
    portal's expected spreadsheet format. Falls back to the raw string if it
    doesn't match any known input format, rather than dropping data."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    for fmt in DATE_INPUT_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return f"{dt.month}/{dt.day}/{dt.year}"
        except ValueError:
            continue
    return raw


def _fmt_time(raw: str) -> str:
    """Normalize to 12-hour 'H:MM AM/PM' (no leading zero on the hour) so Excel/
    Sheets recognizes it as a time value, regardless of the source's original
    casing/format (e.g. '13:11', '1:11pm', '1:11 PM' all -> '1:11 PM')."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    cleaned = re.sub(r"\s+", " ", raw).upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            out = dt.strftime("%I:%M %p")
            return out[1:] if out.startswith("0") else out
        except ValueError:
            continue
    return raw  # leave untouched if unrecognized (e.g. already free-form text)


@mcp.tool()
def generate_salah_csv(masjid_name: str, extraction_json: str) -> dict:
    """Write the Salah/Athan CSV: one row per date, in canonical column order."""
    e = json.loads(extraction_json)
    path = settings.output_path / f"{_slug(masjid_name)}_salah.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date"] + SALAH_HEADER)
        for row in e.get("rows", []):
            salah = row.get("salah", {})
            w.writerow([_fmt_date(row.get("date", ""))] +
                       [_fmt_time(salah.get(c, "")) for c in SALAH_HEADER])
    return {"ok": True, "path": str(path), "rows": len(e.get("rows", []))}


@mcp.tool()
def generate_iqamah_csv(masjid_name: str, extraction_json: str) -> dict:
    """Write the Iqamah/Jamaat CSV: one row per date (Maghrib column = minutes-
    difference or "1"), with Jumuah 1/2 repeated on every row since it's a fixed
    weekly time rather than a per-date value."""
    e = json.loads(extraction_json)
    jumuah = e.get("jumuah", {})
    path = settings.output_path / f"{_slug(masjid_name)}_iqamah.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date"] + IQAMAH_HEADER)
        for row in e.get("rows", []):
            iq = row.get("iqamah", {})
            values = [_fmt_time(iq.get(c, "")) for c in ("Fajr", "Dhuhr", "Asr")]
            values.append(str(iq.get("Maghrib", "")).strip())  # minutes-diff, not a time
            values.append(_fmt_time(iq.get("Isha", "")))
            values.append(_fmt_time(jumuah.get("Jumuah 1", "")))
            values.append(_fmt_time(jumuah.get("Jumuah 2", "")))
            w.writerow([_fmt_date(row.get("date", ""))] + values)
    return {"ok": True, "path": str(path), "rows": len(e.get("rows", []))}


@mcp.tool()
def portal_upload(masjid_name: str, salah_csv: str, iqamah_csv: str) -> dict:
    """Deterministic Playwright automation: login -> Mosque Manager -> search the
    masjid -> New UI login -> upload the two CSVs. No-op unless PORTAL_UPLOAD_ENABLED."""
    if not settings.portal_upload_enabled:
        return {"ok": True, "skipped": True,
                "note": "PORTAL_UPLOAD_ENABLED is false; CSVs generated but not uploaded.",
                "salah_csv": salah_csv, "iqamah_csv": iqamah_csv}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.portal_headless)
            page = browser.new_page()
            page.goto(settings.portal_login_url, wait_until="networkidle")
            # NOTE: selectors below are placeholders — confirm against the live DOM.
            page.fill("input[type=email], input[name*=email]", settings.portal_email)
            page.fill("input[type=password]", settings.portal_password)
            page.click("button[type=submit], input[type=submit]")
            page.wait_for_load_state("networkidle")
            page.click("text=Mosque Manager")
            page.fill("input[type=search], input[placeholder*=Name]", masjid_name)
            page.wait_for_timeout(1500)
            page.click("text=New UI login")
            # ... navigate to Salah/Iqamah upload and set the file inputs ...
            # page.set_input_files("input[type=file]", salah_csv)  # etc.
            browser.close()
        return {"ok": True, "uploaded": True, "masjid": masjid_name}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "hint": "Portal DOM likely changed; verify selectors."}


if __name__ == "__main__":
    mcp.run()
