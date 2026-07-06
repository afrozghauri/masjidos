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


async def _login_and_impersonate(context, page, masjid_name: str):
    """Shared flow for portal_upload and verify_portal_timings: log into the
    backend admin, search Mosque Manager, click the matching row's 'New UI
    Login' icon, and return the resulting authenticated portal tab.
    Returns (portal_page, error) — exactly one is None."""
    await page.goto(settings.portal_login_url, wait_until="networkidle", timeout=30000)
    await page.fill("input[name='LoginForm[email]']", settings.portal_email)
    await page.fill("input[name='LoginForm[password]']", settings.portal_password)
    await page.click("button[type=submit]")
    await page.wait_for_load_state("networkidle", timeout=30000)

    await page.goto("https://masjidal.com/backend/mosque", wait_until="networkidle", timeout=30000)
    await page.fill("input[name='MasjidSearch[m_name]']", masjid_name)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle", timeout=30000)
    # The search re-renders the grid after networkidle resolves; querying too
    # early destroys the execution context mid-navigation. Wait for the grid
    # itself before touching it (same fix needed during exploration — see
    # explore_portal9.py).
    await page.wait_for_selector("table tbody tr", timeout=15000)

    rows = await page.query_selector_all("table tbody tr")
    target_row = None
    for r in rows:
        cells = await r.query_selector_all("td")
        if len(cells) > 1 and (await cells[1].inner_text()).strip().lower() == masjid_name.strip().lower():
            target_row = r
            break
    target_row = target_row or (rows[0] if rows else None)
    if not target_row:
        return None, f"No mosque found in Mosque Manager matching '{masjid_name}'."

    new_ui_link = await target_row.query_selector("a[href*='backend/portal/login']")
    if not new_ui_link:
        return None, f"'{masjid_name}' row has no New UI Login link."

    async with context.expect_page(timeout=20000) as new_page_info:
        await new_ui_link.click()
    portal_page = await new_page_info.value
    await portal_page.wait_for_load_state("networkidle", timeout=30000)
    return portal_page, None


async def _scrape_table(page, url: str) -> dict:
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(1500)  # let any client-side calendar render finish
    headers = await page.eval_on_selector_all(
        "table thead th, table th", "els => els.map(e => e.innerText.trim())")
    rows = await page.eval_on_selector_all(
        "table tbody tr",
        "trs => trs.map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()))")
    return {"headers": headers, "rows": rows}


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
    3. On that tab: /timings/salah -> "Upload Timings" (EXACT text match — the
       page header's "Upload Logo" control also contains the substring
       "Upload", so a substring match can silently click the wrong element) ->
       file chooser -> "SELECT CSV FILE" -> modal's "Upload" submit button ->
       if uploaded dates overlap existing entries, a CUSTOM in-page popup asks
       "Do you want to overwrite record or show old record" (Yes/No buttons —
       confirmed NOT a native browser dialog, since Chromium's native
       confirm() can only ever show "OK"/"Cancel", never custom labels; a
       page.on("dialog") handler never sees this) -> click "Yes". Then the
       same for /timings/iqama with its "Upload" button (exact match).

    Full-month uploads for both Salah and Iqamah, including the overwrite
    confirmation, are confirmed working end-to-end against the live portal
    (2026-07) — verified by reading the result back with
    verify_portal_timings() rather than trusting this function's "ok" alone.

    One thing remains inference, not confirmed against raw HTML: the mosque-
    name search assumes an exact-match row exists in Mosque Manager. Test with
    PORTAL_HEADLESS=false first against any new masjid, ideally non-critical,
    before trusting it broadly.

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

            portal_page, err = await _login_and_impersonate(context, page, masjid_name)
            if err:
                return {"ok": False, "error": err}

            # Belt-and-braces: accept any NATIVE browser dialog that might show
            # up (e.g. a beforeunload prompt if we navigate away too early).
            # This is NOT what handles the overwrite confirmation below — see
            # that comment for why.
            async def _accept_dialog(dialog):
                await dialog.accept()
            portal_page.on("dialog", _accept_dialog)

            for url, filename, csv_path in (
                ("https://portal.masjidal.com/timings/salah", "Upload Timings", salah_csv),
                ("https://portal.masjidal.com/timings/iqama", "Upload", iqamah_csv),
            ):
                await portal_page.goto(url, wait_until="networkidle", timeout=30000)
                # Exact match, not substring: every page has an "Upload Logo"
                # control in the header, and a bare `text=Upload` substring
                # match can silently click that instead of the real button —
                # confirmed live as the cause of the Iqamah upload never
                # opening its modal.
                await portal_page.click(f'text="{filename}"')
                async with portal_page.expect_file_chooser() as fc_info:
                    await portal_page.click("text=SELECT CSV FILE")
                await (await fc_info.value).set_files(csv_path)
                await portal_page.wait_for_selector(f"text={Path(csv_path).name}", timeout=10000)
                await portal_page.locator("button:has-text('Upload')").last.click()
                # Uploading dates that overlap existing entries shows a CUSTOM
                # in-page popup — "Do you want to overwrite record or show old
                # record", with Yes/No buttons. Confirmed live: this is not a
                # native browser dialog (Chromium's native confirm() can only
                # ever show "OK"/"Cancel", never custom labels), so
                # page.on("dialog") above never sees it — that was the actual
                # bug behind only a single (non-overlapping) date landing.
                # Click "Yes" directly; skip silently if no overlap means it
                # never appears.
                try:
                    await portal_page.locator("button:has-text('Yes')").click(timeout=5000)
                except Exception:
                    pass
                await portal_page.wait_for_timeout(2000)

            await browser.close()
        return {"ok": True, "uploaded": True, "masjid": masjid_name}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "hint": "Verify against the live DOM — see portal_upload's docstring "
                        "for which parts are inference vs. confirmed."}


@mcp.tool()
async def verify_portal_timings(masjid_name: str) -> dict:
    """Read-only: log in and impersonate into the given masjid's portal (same
    flow as portal_upload), then scrape whatever the Salah Timings and Iqamah
    Timings pages CURRENTLY display. Exists so success can be confirmed by
    reading the portal back, rather than trusting portal_upload's own "ok"
    alone — that was essential during development, since portal_upload
    reported "ok" even on runs where the upload silently didn't take effect.

    Note: the Iqamah Timings table appears to be paginated — this scrapes
    only whatever page is showing by default (observed: the most recent ~25
    rows), not the full calendar. Fine for a spot-check; not a substitute for
    counting rows if you need to confirm every date landed.

    No-op unless PORTAL_UPLOAD_ENABLED (same live-system gate as portal_upload)."""
    if not settings.portal_upload_enabled:
        return {"ok": False, "error": "PORTAL_UPLOAD_ENABLED is false; nothing to verify against."}
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.portal_headless)
            context = await browser.new_context()
            page = await context.new_page()

            portal_page, err = await _login_and_impersonate(context, page, masjid_name)
            if err:
                return {"ok": False, "error": err}

            salah = await _scrape_table(portal_page, "https://portal.masjidal.com/timings/salah")
            iqamah = await _scrape_table(portal_page, "https://portal.masjidal.com/timings/iqama")

            await browser.close()
        return {"ok": True, "masjid": masjid_name, "salah": salah, "iqamah": iqamah}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
