"""Streamlit human review gate.

Shows the queue. High-confidence items are auto-published; low-confidence items the
agent chose to escalate appear here with the agent's own reasoning, per-field
confidence, and an Approve action. Kept intentionally plain — it's an internal tool.
"""
import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.append(str(Path(__file__).resolve().parent.parent))
from agent.run import Session, ReviewItem              # noqa: E402
from mcp_servers.publishing_server import (            # noqa: E402
    generate_salah_csv, generate_iqamah_csv, portal_upload)

import asyncio
import tempfile

st.set_page_config(page_title="MasjidOS Review Gate", layout="wide")
st.title("MasjidOS — Timetable Review Gate")
st.caption("Only cases the agent judged too uncertain to auto-publish need you.")

st.divider()
st.subheader("1. Load the masjid directory")

mode = st.radio("Source", ["Google Sheet link", "Upload Excel/CSV file"], horizontal=True)

source = None
if mode == "Google Sheet link":
    link = st.text_input(
        "Paste the Google Sheet share link",
        placeholder="https://docs.google.com/spreadsheets/d/....../edit",
    )
    st.caption("Share the sheet as **Editor** with your service account's "
               "client_email (see .env) — no public link sharing is needed.")
    if link:
        source = link
else:
    uploaded = st.file_uploader("Choose a masjid directory file", type=["xlsx", "xls", "csv"])
    if uploaded:
        suffix = Path(uploaded.name).suffix
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(uploaded.getbuffer())
        tmp.close()
        source = tmp.name
        st.caption(f"Loaded: {uploaded.name}")

col1, col2, col3 = st.columns([1, 1, 3])
with col1:
    demo_limit = st.number_input("Limit to first N masjids", min_value=1, max_value=50,
                                 value=2, step=1)
with col2:
    run_clicked = st.button("Load & Run", type="primary", disabled=not source)

if run_clicked and source:
    from agent.run import process_directory
    with st.spinner(f"Reading directory and processing the first {demo_limit} masjid(s)..."):
        results = asyncio.run(process_directory(source, int(demo_limit)))
    if results and "error" in results[0]:
        st.error(results[0]["error"])
    else:
        st.success(f"Processed {len(results)} masjid(s). See the queue below.")
        st.rerun()

st.divider()
st.subheader("2. Review queue")


def load():
    s = Session()
    items = s.query(ReviewItem).order_by(ReviewItem.id.desc()).all()
    data = [(i.id, i.masjid_name, i.status, i.min_confidence,
             i.rationale, i.extraction_json, i.source, i.row_number,
             i.trace_json) for i in items]
    s.close()
    return data


items = load()
if not items:
    st.info("No runs yet. Start one with:  `python -m agent.run --name X --url https://...`")
    st.stop()

for _id, name, status, mc, rationale, ej, item_source, item_row, trace_json in items:
    color = {"AUTO_PUBLISHED": "🟢", "NEEDS_REVIEW": "🟡",
             "APPROVED": "✅", "ERROR": "🔴"}.get(status, "⚪")
    with st.expander(f"{color} {name} — {status} (min confidence {mc:.2f})",
                     expanded=(status == "NEEDS_REVIEW")):
        try:
            e = json.loads(ej)
        except Exception:
            st.error(ej); continue

        st.markdown(f"**Detected scheme:** {e.get('detected_label_scheme','?')} "
                    f"· **Maghrib single column:** {e.get('maghrib_single_column','?')}")
        st.markdown(f"**Agent reasoning:** {e.get('rationale', rationale)}")

        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Salah (Athan)")
            st.dataframe(pd.DataFrame([
                {"Prayer": k, "Time": v.get("value"), "Conf": v.get("confidence")}
                for k, v in e.get("salah", {}).items()]), hide_index=True)
        with c2:
            st.subheader("Iqamah (Jamaat)")
            st.dataframe(pd.DataFrame([
                {"Prayer": k, "Value": v.get("value"), "Conf": v.get("confidence")}
                for k, v in e.get("iqamah", {}).items()]), hide_index=True)

        with st.expander("🔍 Agent reasoning trace (tool calls in order)"):
            try:
                trace = json.loads(trace_json) if trace_json else []
            except Exception:
                trace = []
            if not trace:
                st.caption("No trace recorded for this run.")
            for step in trace:
                if step["type"] == "tool_call":
                    st.markdown(f"**→ called `{step['tool']}`** with `{step['args']}`")
                else:
                    st.code(step["content"], language="json")

        if status in ("NEEDS_REVIEW", "AUTO_PUBLISHED"):
            if st.button("Approve & generate CSVs", key=f"ap{_id}"):
                from mcp_servers.acquisition_server import mark_row_done
                salah = generate_salah_csv(name, ej)
                iqamah = generate_iqamah_csv(name, ej)
                up = portal_upload(name, salah["path"], iqamah["path"])

                mark = {"ok": False, "error": "no source/row recorded for this item"}
                if item_source and item_row:
                    mark = mark_row_done(item_source, item_row)

                s = Session()
                it = s.query(ReviewItem).get(_id)
                it.status = "APPROVED"; s.commit(); s.close()

                st.success(f"CSVs written. Upload: {up.get('note', up)}")
                if mark.get("ok"):
                    st.success(f"Marked Done — row {mark['row']} in {mark.get('sheet', mark.get('file'))}")
                else:
                    st.warning(f"Could not mark Done in the sheet: {mark.get('error')}")
