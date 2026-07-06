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

# Header text matches Masjidal's own downloadable CSV templates exactly (Salah:
# Masjidal_Salah_CSV_Template, Iqamah: Masjidal_Iqama_CSV_Template) — confirmed
# 2026-07, including the verbatim Maghrib/Jumu'ah column labels below.
SALAH_HEADER = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
IQAMAH_HEADER = ["Fajr", "Dhuhr", "Asr", "Maghrib (minutes after Salah start time)",
                 "Isha", "Jumu'ah I", "Jumu'ah II"]
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
    """Normalize to zero-padded 12-hour 'HH:MM AM/PM' — matches Masjidal's own
    sample CSV templates exactly (e.g. '05:16 AM', '12:41 PM'), regardless of the
    source's original casing/format (e.g. '13:11', '1:11pm', '1:11 PM')."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    cleaned = re.sub(r"\s+", " ", raw).upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%I:%M %p")
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
async def portal_upload(masjid_name: str, salah_csv: str, iqamah_csv: str) -> dict:
    """Deterministic Playwright automation against the REAL Masjidal system,
    confirmed against the live DOM (2026-07):

    1. Log into the backend admin (masjidal.com/backend/site/login) with
       PORTAL_EMAIL/PORTAL_PASSWORD.
    2. Mosque Manager (backend/mosque) -> search by masjid_name -> the matching
       row's "New UI Login" icon opens a NEW TAB (window.open) that lands,
       already authenticated via a one-time JWT, on the mosque-facing frontend
       at portal.masjidal.com/dashboard/.
    3. On that tab: /timings/salah -> "Upload Timings" -> file chooser ->
       "Upload"; then /timings/iqama -> "Upload" -> file chooser -> "Upload".

    Two things are inference, not confirmed against raw HTML (no direct DOM
    access was available while building this — only screenshots): the exact
    modal "Upload" submit button (there are TWO buttons with overlapping text,
    the nav button that opens the modal and the modal's own submit button; we
    take the LAST match on the theory that modals are appended to the DOM
    after their trigger), and the mosque-name search assumes an exact-match
    row exists. Test with PORTAL_HEADLESS=false first to watch it run, ideally
    against a non-critical masjid, before trusting it broadly.

    No-op unless PORTAL_UPLOAD_ENABLED."""
    if not settings.portal_upload_enabled:
        return {"ok": True, "skipped": True,
                "note": "PORTAL_UPLOAD_ENABLED is false; CSVs generated but not uploaded.",
                "salah_csv": salah_csv, "iqamah_csv": iqamah_csv}
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.portal_headless)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(settings.portal_login_url, wait_until="networkidle", timeout=30000)
            await page.fill("input[name='LoginForm[email]']", settings.portal_email)
            await page.fill("input[name='LoginForm[password]']", settings.portal_password)
            await page.click("button[type=submit]")
            await page.wait_for_load_state("networkidle", timeout=30000)

            await page.goto("https://masjidal.com/backend/mosque", wait_until="networkidle", timeout=30000)
            await page.fill("input[name='MasjidSearch[m_name]']", masjid_name)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=30000)

            rows = await page.query_selector_all("table tbody tr")
            target_row = None
            for r in rows:
                cells = await r.query_selector_all("td")
                if len(cells) > 1 and (await cells[1].inner_text()).strip().lower() == masjid_name.strip().lower():
                    target_row = r
                    break
            target_row = target_row or (rows[0] if rows else None)
            if not target_row:
                return {"ok": False, "error": f"No mosque found in Mosque Manager matching '{masjid_name}'."}

            new_ui_link = await target_row.query_selector("a[href*='backend/portal/login']")
            if not new_ui_link:
                return {"ok": False, "error": f"'{masjid_name}' row has no New UI Login link."}

            async with context.expect_page(timeout=20000) as new_page_info:
                await new_ui_link.click()
            portal_page = await new_page_info.value
            await portal_page.wait_for_load_state("networkidle", timeout=30000)

            for url, filename, csv_path in (
                ("https://portal.masjidal.com/timings/salah", "Upload Timings", salah_csv),
                ("https://portal.masjidal.com/timings/iqama", "Upload", iqamah_csv),
            ):
                await portal_page.goto(url, wait_until="networkidle", timeout=30000)
                await portal_page.click(f"text={filename}")
                async with portal_page.expect_file_chooser() as fc_info:
                    await portal_page.click("text=SELECT CSV FILE")
                await (await fc_info.value).set_files(csv_path)
                await portal_page.wait_for_selector(f"text={Path(csv_path).name}", timeout=10000)
                await portal_page.locator("button:has-text('Upload')").last.click()
                await portal_page.wait_for_timeout(2000)

            await browser.close()
        return {"ok": True, "uploaded": True, "masjid": masjid_name}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "hint": "Verify against the live DOM — see portal_upload's docstring "
                        "for which parts are inference vs. confirmed."}


if __name__ == "__main__":
    mcp.run()
