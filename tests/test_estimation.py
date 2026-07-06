"""Tests for the missing-data fallback logic: trend extrapolation (Salah),
carry-forward (Iqamah/Jumuah), and merging chunked extractions (yearly
sources). All pure/dependency-free — no MCP or network needed."""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from agent.validation import (
    minutes_to_clock, extrapolate_salah_trend, carry_forward_last_value,
    carry_forward_jumuah, merge_extractions)


def test_minutes_to_clock_roundtrip():
    assert minutes_to_clock(312) == "5:12 AM"
    assert minutes_to_clock(810) == "1:30 PM"
    assert minutes_to_clock(0) == "12:00 AM"
    assert minutes_to_clock(12 * 60) == "12:00 PM"


def test_extrapolate_salah_trend_projects_linear_shift():
    # Fajr getting 1 minute earlier each day (typical for this time of year).
    history = [
        {"date": "2026-07-01", "Fajr": "3:00 AM"},
        {"date": "2026-07-02", "Fajr": "2:59 AM"},
        {"date": "2026-07-03", "Fajr": "2:58 AM"},
    ]
    out = extrapolate_salah_trend(history, ["2026-07-04", "2026-07-05"])
    assert out["2026-07-04"]["Fajr"] == "2:57 AM"
    assert out["2026-07-05"]["Fajr"] == "2:56 AM"


def test_extrapolate_salah_trend_skips_column_with_insufficient_history():
    history = [{"date": "2026-07-01", "Fajr": "3:00 AM"}]  # only one data point
    out = extrapolate_salah_trend(history, ["2026-07-02"])
    assert "Fajr" not in out["2026-07-02"]


def test_carry_forward_last_value_uses_most_recent_iqamah():
    history = [
        {"date": "2026-06-28", "Fajr": "4:00 AM", "Dhuhr": "1:15 PM"},
        {"date": "2026-06-30", "Fajr": "4:05 AM", "Dhuhr": "1:15 PM"},
    ]
    out = carry_forward_last_value(history, ["2026-07-01", "2026-07-02"], columns=["Fajr", "Dhuhr"])
    assert out["2026-07-01"] == {"Fajr": "4:05 AM", "Dhuhr": "1:15 PM"}
    assert out["2026-07-02"] == {"Fajr": "4:05 AM", "Dhuhr": "1:15 PM"}


def test_carry_forward_last_value_no_history_returns_empty():
    out = carry_forward_last_value([], ["2026-07-01"], columns=["Fajr"])
    assert out["2026-07-01"] == {}


def test_carry_forward_jumuah_keeps_nonempty_values():
    out = carry_forward_jumuah({"Jumuah 1": "1:30 PM", "Jumuah 2": ""})
    assert out == {"Jumuah 1": "1:30 PM"}


def test_merge_extractions_concatenates_and_sorts_rows():
    chunk_a = {"masjid_name": "X", "rationale": "a", "overall_confidence": 0.9,
              "column_confidence": {"salah": {"Fajr": 0.9}, "iqamah": {}},
              "not_applicable": {"salah": {}, "iqamah": {}}, "jumuah": {},
              "rows": [{"date": "2026-02-01"}, {"date": "2026-02-02"}]}
    chunk_b = {"masjid_name": "X", "rationale": "b", "overall_confidence": 0.7,
              "column_confidence": {"salah": {"Fajr": 0.6}, "iqamah": {}},
              "not_applicable": {"salah": {}, "iqamah": {}}, "jumuah": {"Jumuah 1": "1:30 PM"},
              "rows": [{"date": "2026-01-01"}, {"date": "2026-01-02"}]}
    merged = merge_extractions([chunk_a, chunk_b])
    assert [r["date"] for r in merged["rows"]] == ["2026-01-01", "2026-01-02", "2026-02-01", "2026-02-02"]
    assert merged["column_confidence"]["salah"]["Fajr"] == 0.6  # min across chunks
    assert merged["jumuah"] == {"Jumuah 1": "1:30 PM"}  # from the chunk that has it
    assert merged["overall_confidence"] == 0.7


def test_merge_extractions_not_applicable_requires_all_chunks_agree():
    chunk_a = {"not_applicable": {"salah": {}, "iqamah": {"Jumuah 2": True}}, "rows": []}
    chunk_b = {"not_applicable": {"salah": {}, "iqamah": {"Jumuah 2": False}}, "rows": []}
    merged = merge_extractions([chunk_a, chunk_b])
    assert merged["not_applicable"]["iqamah"]["Jumuah 2"] is False


def test_merge_extractions_empty_list():
    assert merge_extractions([]) == {}
