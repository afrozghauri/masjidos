"""Controller: runs the agent per masjid, applies the confidence gate, and either
auto-publishes (high confidence) or escalates to the human review queue (low).

This is where the CORE AGENTIC DECISION is made and made AUDITABLE: the choice to
trust or distrust the agent's own output lives here, not hidden in the LLM.
"""
import asyncio
import json
from pathlib import Path

from loguru import logger
from sqlalchemy import create_engine, Column, String, Float, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.settings import settings          # noqa: E402
from agent.graph import comprehend            # noqa: E402
from mcp_servers.publishing_server import (   # noqa: E402
    generate_salah_csv, generate_iqamah_csv, portal_upload)

Base = declarative_base()
engine = create_engine(settings.database_url)
Session = sessionmaker(bind=engine)


class ReviewItem(Base):
    __tablename__ = "review_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    masjid_name = Column(String)
    url = Column(String)
    status = Column(String)          # AUTO_PUBLISHED | NEEDS_REVIEW | APPROVED | ERROR
    min_confidence = Column(Float)
    extraction_json = Column(Text)
    rationale = Column(Text)
    source = Column(String)          # sheet link or file path the row came from
    row_number = Column(Integer)     # 1-indexed row in that source
    trace_json = Column(Text)        # step-by-step tool-call trace, for the demo          # sheet link or file path the row came from


Base.metadata.create_all(engine)


def _min_conf(extraction: dict) -> float:
    cc = extraction.get("column_confidence", {})
    na = extraction.get("not_applicable", {})
    confs = []
    for side in ("salah", "iqamah"):
        for field, conf in cc.get(side, {}).items():
            if na.get(side, {}).get(field):
                continue
            confs.append(conf)
    return min(confs) if confs else 0.0


async def process_masjid(name: str, url: str, source: str = "", row_number: int = 0) -> dict:
    logger.info(f"Processing {name}")
    try:
        raw, trace = await comprehend(name, url)
        # The agent returns JSON possibly wrapped in prose/backticks — extract it.
        start, end = raw.find("{"), raw.rfind("}")
        extraction = json.loads(raw[start:end + 1])
    except Exception as e:
        logger.error(f"{name}: {e}")
        _save(name, url, "ERROR", 0.0, {"error": str(e)}, str(e), source, row_number, [])
        return {"name": name, "status": "ERROR", "error": str(e)}

    mc = _min_conf(extraction)
    has_estimates = bool(extraction.get("estimated_fields"))
    publish_result = None
    if mc >= settings.confidence_threshold and not has_estimates:
        status = "AUTO_PUBLISHED"
        logger.success(f"{name}: auto-published (min conf {mc:.2f})")
        ej = json.dumps(extraction)
        salah = generate_salah_csv(name, ej)
        iqamah = generate_iqamah_csv(name, ej)
        upload = await portal_upload(name, salah["path"], iqamah["path"])
        mark = {"ok": False, "error": "no source/row recorded for this item"}
        if source and row_number:
            from mcp_servers.acquisition_server import mark_row_done
            mark = mark_row_done(source, row_number)
        publish_result = {"salah": salah, "iqamah": iqamah, "upload": upload, "marked_done": mark}
        logger.info(f"{name}: CSVs written -> {salah['path']}, {iqamah['path']}")
    else:
        status = "NEEDS_REVIEW"
        if has_estimates:
            logger.warning(f"{name}: escalated for review — contains estimated "
                           f"fields (not real readings): {extraction['estimated_fields']}")
        else:
            logger.warning(f"{name}: escalated for review (min conf {mc:.2f})")

    _save(name, url, status, mc, extraction, extraction.get("rationale", ""),
         source, row_number, trace)
    result = {"name": name, "status": status, "min_confidence": mc}
    if publish_result:
        result["publish"] = publish_result
    return result


def _save(name, url, status, mc, extraction, rationale, source="", row_number=0, trace=None):
    s = Session()
    s.add(ReviewItem(masjid_name=name, url=url, status=status,
                     min_confidence=mc, extraction_json=json.dumps(extraction),
                     rationale=rationale, source=source, row_number=row_number,
                     trace_json=json.dumps(trace or [])))
    s.commit()
    s.close()


async def process_directory(source: str = "", limit: int = 0):
    """Run every masjid in the input spreadsheet via the Acquisition read_directory
    tool (so even the input list is loaded through MCP). `source` can be a Google
    Sheets link or a local file path; empty uses the .env default. `limit` caps how
    many masjids are processed (0 = no limit). 'Done' is NOT written here — it is
    only written once a human clicks Approve in the review gate."""
    from mcp_servers.acquisition_server import read_directory
    masjids = read_directory(source, limit)
    if masjids and "error" in masjids[0]:
        logger.error(masjids[0]["error"])
        return [masjids[0]]
    results = []
    for m in masjids:
        if "error" in m:
            logger.error(m["error"]); continue
        r = await process_masjid(m["name"], m["url"], m["source"], m["row_number"])
        results.append(r)
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--name")
    ap.add_argument("--url")
    ap.add_argument("--source", default="",
                    help="Google Sheets link or local file path (overrides .env)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max number of masjids to process (0 = no limit)")
    args = ap.parse_args()
    if args.name and args.url:
        print(asyncio.run(process_masjid(args.name, args.url)))
    else:
        print(asyncio.run(process_directory(args.source, args.limit)))
