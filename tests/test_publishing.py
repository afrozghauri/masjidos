"""Tests for the deterministic CSV-formatting logic in publishing_server.py —
specifically _fmt_time's AM/PM resolution, since a bug here already reached a
real masjid's live portal once (Dhuhr with no AM/PM label misread as 1:13 AM
instead of 1:13 PM)."""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from mcp_servers.publishing_server import _fmt_time


def test_explicit_am_pm_is_authoritative():
    assert _fmt_time("2:53 am") == "02:53 AM"
    assert _fmt_time("9:25 pm") == "09:25 PM"
    assert _fmt_time("1:11 PM") == "01:11 PM"


def test_unlabeled_time_resolves_from_column():
    # Observed live: Reading Islamic Center's source omits AM/PM entirely,
    # relying on the reader knowing Dhuhr/Asr/Isha are afternoon/evening.
    assert _fmt_time("01:13", column="Dhuhr") == "01:13 PM"
    assert _fmt_time("06:44", column="Asr") == "06:44 PM"
    assert _fmt_time("10:42", column="Isha") == "10:42 PM"
    assert _fmt_time("11:00", column="Isha") == "11:00 PM"
    assert _fmt_time("12:30", column="Jumuah 1") == "12:30 PM"


def test_unlabeled_time_am_columns():
    assert _fmt_time("02:54", column="Fajr") == "02:54 AM"
    assert _fmt_time("04:15", column="Sunrise") == "04:15 AM"


def test_unlabeled_time_no_column_falls_back_to_24h():
    # No column context at all: can't apply domain knowledge, so this is the
    # best available fallback (plain 24-hour reading).
    assert _fmt_time("13:11") == "01:11 PM"


def test_empty_and_unparseable():
    assert _fmt_time("") == ""
    assert _fmt_time(None) == ""
    assert _fmt_time("free text") == "free text"
