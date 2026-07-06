"""Tests for the deterministic validator — the parts that MUST be reliable.
Imports the pure module so no MCP/LLM deps are needed."""
import json
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from agent.validation import validate_json, to_minutes, compute_maghrib_iqamah


def test_time_parse():
    assert to_minutes("5:12 AM") == 312
    assert to_minutes("1:30 PM") == 810
    assert to_minutes("12:00 AM") == 0
    assert to_minutes("garbage") is None


def test_maghrib_iqamah_computes_difference():
    e = {"rows": [{"date": "2026-07-01",
                   "salah": {"Maghrib": "9:25 PM"},
                   "iqamah": {"Maghrib": "9:28 PM"}}]}
    out = compute_maghrib_iqamah(e)
    assert out["rows"][0]["iqamah"]["Maghrib"] == "3"


def test_maghrib_iqamah_defaults_to_one_when_equal():
    e = {"rows": [{"date": "2026-07-01",
                   "salah": {"Maghrib": "9:25 PM"},
                   "iqamah": {"Maghrib": "9:25 PM"}}]}
    out = compute_maghrib_iqamah(e)
    assert out["rows"][0]["iqamah"]["Maghrib"] == "1"


def test_maghrib_iqamah_defaults_to_one_when_missing():
    e = {"rows": [{"date": "2026-07-01",
                   "salah": {"Maghrib": "9:25 PM"},
                   "iqamah": {"Maghrib": ""}}]}
    out = compute_maghrib_iqamah(e)
    assert out["rows"][0]["iqamah"]["Maghrib"] == "1"


def test_maghrib_iqamah_leaves_existing_diff_alone():
    e = {"rows": [{"date": "2026-07-01",
                   "salah": {"Maghrib": "9:25 PM"},
                   "iqamah": {"Maghrib": "5"}}]}
    out = compute_maghrib_iqamah(e)
    assert out["rows"][0]["iqamah"]["Maghrib"] == "5"


def test_maghrib_must_be_minutes_not_clock():
    e = {"rows": [{"date": "2026-07-01", "salah": {}, "iqamah": {"Maghrib": "9:15 PM"}}]}
    r = validate_json(json.dumps(e))
    assert not r["consistent"]
    assert any("minutes-difference" in i for i in r["issues"])


def test_chronological_flag():
    e = {"rows": [{"date": "2026-07-01", "salah": {
        "Fajr": "5:00 AM", "Dhuhr": "1:00 PM", "Asr": "11:00 AM",
    }, "iqamah": {"Maghrib": "1"}}]}
    r = validate_json(json.dumps(e))
    assert not r["consistent"]


def test_clean_passes():
    e = {"rows": [{"date": "2026-07-01", "salah": {
        "Fajr": "5:00 AM", "Dhuhr": "1:00 PM", "Asr": "5:30 PM",
    }, "iqamah": {"Maghrib": "1"}}]}
    r = validate_json(json.dumps(e))
    assert r["consistent"]
