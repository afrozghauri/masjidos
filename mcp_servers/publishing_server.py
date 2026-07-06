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
from agent.validation import (  # noqa: E402
    extrapolate_salah_trend, carry_forward_last_value, carry_forward_jumuah)

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


_AM_COLUMNS = {"Fajr", "Sunrise"}
_PM_COLUMNS = {"Dhuhr", "Asr", "Maghrib", "Isha", "Jumuah 1", "Jumuah 2"}


def _fmt_time(raw: str, column: str | None = None) -> str:
    """Normalize to zero-padded 12-hour 'HH:MM AM/PM' — matches Masjidal's own
    sample CSV templates exactly (e.g. '05:16 AM', '12:41 PM'), regardless of
    the source's original casing/format (e.g. '13:11', '1:11pm', '1:11 PM').

    `column` matters when the source omits AM/PM entirely — some tables show
    e.g. "01:13" for Dhuhr, relying on the reader to know Dhuhr is early
    afternoon rather than labeling it PM. Domain knowledge is reliable here:
    Fajr/Sunrise are always AM; Dhuhr/Asr/Maghrib/Isha/Jumuah are always PM.
    Observed live without this: an unlabeled Dhuhr "01:13" got parsed as a
    bare 24-hour reading (1:13 AM) — wrong, and it reached a real masjid's
    live portal before being caught. Resolve from the column instead of
    guessing 24-hour."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    cleaned = re.sub(r"\s+", " ", raw).upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%I:%M %p")
        except ValueError:
            continue
    if re.match(r"^\d{1,2}:\d{2}$", cleaned):
        suffix = "AM" if column in _AM_COLUMNS else "PM" if column in _PM_COLUMNS else None
        if suffix:
            try:
                dt = datetime.strptime(f"{cleaned} {suffix}", "%I:%M %p")
                return dt.strftime("%I:%M %p")
            except ValueError:
                pass
    try:  # no column context (or already looks 24-hour, e.g. "13:11") — fall
          # back to a plain 24-hour reading.
        dt = datetime.strptime(cleaned, "%H:%M")
        return dt.strftime("%I:%M %p")
    except ValueError:
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
                       [_fmt_time(salah.get(c, ""), column=c) for c in SALAH_HEADER])
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
            values = [_fmt_time(iq.get(c, ""), column=c) for c in ("Fajr", "Dhuhr", "Asr")]
            values.append(str(iq.get("Maghrib", "")).strip())  # minutes-diff, not a time
            values.append(_fmt_time(iq.get("Isha", ""), column="Isha"))
            values.append(_fmt_time(jumuah.get("Jumuah 1", ""), column="Jumuah 1"))
            values.append(_fmt_time(jumuah.get("Jumuah 2", ""), column="Jumuah 2"))
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


_PORTAL_DATE_FORMATS = ("%d-%b-%Y", "%d %b, %Y", "%d %B, %Y", "%d-%B-%Y")
_PORTAL_SALAH_COLUMNS = ["date"] + SALAH_HEADER
_PORTAL_IQAMAH_COLUMNS = ["date", "Fajr", "Dhuhr", "Asr", "Maghrib", "Isha",
                         "Jumuah 1", "Jumuah 2", "Jumuah 3", "_action"]


def _parse_portal_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in _PORTAL_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


_PORTAL_EMPTY_PLACEHOLDERS = {"-", "—", "n/a", "na", ""}
# The portal itself displays "-" for a timing that was never set (observed
# live: Shah Jahan Mosque's "Jumuah II"/"Jumuah III" columns show "-"). Must
# be treated as absent, not carried forward as if it were a real value.


def _portal_rows_to_dicts(rows: list, columns: list[str]) -> list[dict]:
    """Turn verify_portal_timings' positional scraped rows into the
    {"date": ..., "Fajr": ..., ...} shape agent.validation's trend/carry-
    forward functions expect. Columns starting with '_' are ignored (e.g. the
    Iqamah table's trailing Action-icons cell, which isn't a real value)."""
    out = []
    for r in rows:
        d = {}
        for col, val in zip(columns, r):
            if col == "date":
                d["date"] = _parse_portal_date(val)
            elif not col.startswith("_") and val.strip().lower() not in _PORTAL_EMPTY_PLACEHOLDERS:
                d[col] = val
        out.append(d)
    return out


@mcp.tool()
async def fill_missing_from_portal_history(masjid_name: str, extraction_json: str) -> dict:
    """When the masjid's own website is entirely missing Salah, Iqamah, and/or
    Jumuah data for one or more columns (genuinely never found — not merely
    low-confidence, and not not_applicable), fall back to the masjid's OWN
    existing data already on the Masjidal portal as a baseline:

    - Salah: extrapolate the recent day-over-day TREND (smooth — Salah times
      are astronomically driven) via extrapolate_salah_trend.
    - Iqamah (Fajr/Dhuhr/Asr/Isha): CARRY FORWARD the most recent known value,
      no smoothing — these are set by mosque admins and change in occasional
      steps, not a trend.
    - Jumuah: CARRY FORWARD the most recent known value(s) — a fixed weekly
      time that rarely changes.

    Every field filled this way is recorded in extraction["estimated_fields"]
    (e.g. ["iqamah.Fajr", "jumuah.Jumuah 1"]) — this is an ESTIMATE, not a
    real reading, so the confidence gate in agent/run.py checks for this and
    always escalates to human review regardless of confidence score.

    No-op (extraction returned unchanged) if nothing is actually missing, or
    if PORTAL_UPLOAD_ENABLED is false (can't reach the portal for history)."""
    extraction = json.loads(extraction_json)
    if not settings.portal_upload_enabled:
        return {"ok": True, "extraction": extraction,
                "note": "PORTAL_UPLOAD_ENABLED is false; cannot fetch portal history."}

    rows = extraction.get("rows", [])
    if not rows:
        return {"ok": True, "extraction": extraction}

    na = extraction.get("not_applicable", {})

    def _column_missing(side, col):
        if na.get(side, {}).get(col):
            return False  # genuinely not applicable, not "missing"
        return not any(str(r.get(side, {}).get(col, "")).strip() for r in rows)

    missing_salah_cols = [c for c in SALAH_HEADER if _column_missing("salah", c)]
    missing_iqamah_cols = [c for c in ("Fajr", "Dhuhr", "Asr", "Isha") if _column_missing("iqamah", c)]
    jumuah = extraction.get("jumuah", {})
    missing_jumuah = [k for k in ("Jumuah 1", "Jumuah 2")
                      if not na.get("iqamah", {}).get(k) and not str(jumuah.get(k, "")).strip()]

    if not (missing_salah_cols or missing_iqamah_cols or missing_jumuah):
        return {"ok": True, "extraction": extraction, "note": "Nothing missing; no fallback needed."}

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.portal_headless)
            context = await browser.new_context()
            page = await context.new_page()
            portal_page, err = await _login_and_impersonate(context, page, masjid_name)
            if err:
                return {"ok": False, "error": err}

            salah_scrape = (await _scrape_table(portal_page, "https://portal.masjidal.com/timings/salah")
                            if missing_salah_cols else None)
            iqamah_scrape = (await _scrape_table(portal_page, "https://portal.masjidal.com/timings/iqama")
                             if (missing_iqamah_cols or missing_jumuah) else None)
            await browser.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    dates = [r.get("date", "") for r in rows]
    estimated_fields = []

    if missing_salah_cols and salah_scrape:
        history = _portal_rows_to_dicts(salah_scrape.get("rows", []), _PORTAL_SALAH_COLUMNS)
        filled = extrapolate_salah_trend(history, dates)
        for row in rows:
            for col in missing_salah_cols:
                val = filled.get(row.get("date", ""), {}).get(col)
                if val:
                    row.setdefault("salah", {})[col] = val
        estimated_fields.extend(f"salah.{c}" for c in missing_salah_cols)

    if iqamah_scrape:
        history = _portal_rows_to_dicts(iqamah_scrape.get("rows", []), _PORTAL_IQAMAH_COLUMNS)
        if missing_iqamah_cols:
            filled = carry_forward_last_value(history, dates, columns=missing_iqamah_cols)
            for row in rows:
                for col in missing_iqamah_cols:
                    val = filled.get(row.get("date", ""), {}).get(col)
                    if val:
                        row.setdefault("iqamah", {})[col] = val
            estimated_fields.extend(f"iqamah.{c}" for c in missing_iqamah_cols)

        if missing_jumuah and history:
            last = sorted(history, key=lambda r: r.get("date", ""))[-1]
            filled_jumuah = carry_forward_jumuah(
                {"Jumuah 1": last.get("Jumuah 1", ""), "Jumuah 2": last.get("Jumuah 2", "")})
            for k in missing_jumuah:
                if filled_jumuah.get(k):
                    jumuah[k] = filled_jumuah[k]
                    estimated_fields.append(f"jumuah.{k}")
            extraction["jumuah"] = jumuah

    if estimated_fields:
        extraction["estimated_fields"] = sorted(set(extraction.get("estimated_fields", [])) | set(estimated_fields))
        extraction["rationale"] = (
            extraction.get("rationale", "") +
            f" [FILLED FROM PORTAL HISTORY: {', '.join(estimated_fields)} were "
            f"missing from the website entirely; filled from this masjid's own "
            f"existing portal data (carried forward/trend-extrapolated, not "
            f"extracted) — flagged for mandatory human review.]")

    return {"ok": True, "extraction": extraction, "estimated_fields": estimated_fields}


if __name__ == "__main__":
    mcp.run()
