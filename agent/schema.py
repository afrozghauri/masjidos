"""Canonical schema the agent maps every source into.

The source is a CALENDAR of dates (a full month, typically), so the extraction
holds one row per date plus column-level (not per-cell) confidence — a masjid's
Fajr column is either well-mapped across the whole month or it isn't.

Salah (Athan / Begins) order:  Fajr, Sunrise, Dhuhr, Asr, Maghrib, Isha
Iqamah (Jamaat) order:         Fajr, Dhuhr, Asr, Maghrib*, Isha
Jumuah (Friday prayer) order:  Jumuah 1, Jumuah 2 — reported ONCE, not per-date,
                                since Jumuah is a fixed weekly time, not daily.

* Maghrib in the Iqamah sheet is NOT a clock time. It is the number of minutes
  between the Maghrib begin (Salah) time and the Maghrib jamaat (Iqamah) time.
  If Maghrib has only one column, or begin == jamaat, the value is "1".
"""
from pydantic import BaseModel, Field

SALAH_COLUMNS = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
IQAMAH_COLUMNS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
JUMUAH_COLUMNS = ["Jumuah 1", "Jumuah 2"]


class DayRow(BaseModel):
    date: str                            # "YYYY-MM-DD"
    salah: dict[str, str] = Field(default_factory=dict)
    iqamah: dict[str, str] = Field(default_factory=dict)


class ColumnConfidence(BaseModel):
    salah: dict[str, float] = Field(default_factory=dict)
    iqamah: dict[str, float] = Field(default_factory=dict)  # includes Jumuah 1/2


class NotApplicable(BaseModel):
    salah: dict[str, bool] = Field(default_factory=dict)
    iqamah: dict[str, bool] = Field(default_factory=dict)   # e.g. {"Jumuah 2": true}


class TimetableExtraction(BaseModel):
    """What the Comprehension MCP returns to the agent."""
    masjid_name: str
    detected_label_scheme: str           # e.g. "Begins/Jamaat", "Athan/Iqamah"
    maghrib_single_column: bool
    rationale: str                       # short explanation of the agent's mapping
    overall_confidence: float = Field(0.0, ge=0.0, le=1.0)
    column_confidence: ColumnConfidence
    not_applicable: NotApplicable = Field(default_factory=NotApplicable)
    jumuah: dict[str, str] = Field(default_factory=dict)    # {"Jumuah 1": ..., "Jumuah 2": ...}
    rows: list[DayRow] = Field(default_factory=list)

    def min_confidence(self) -> float:
        na = self.not_applicable
        confs = [v for f, v in self.column_confidence.salah.items()
                 if not na.salah.get(f)]
        confs += [v for f, v in self.column_confidence.iqamah.items()
                  if not na.iqamah.get(f)]
        return min(confs) if confs else 0.0
