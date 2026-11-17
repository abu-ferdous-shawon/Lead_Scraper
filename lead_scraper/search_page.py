"""
Business Lead Finder - simple web UI around maps_scraper.py
Run with:  streamlit run app.py   (or just double-click run.bat on Windows)
"""

import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)
SCRAPER = HERE / "maps_scraper.py"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"



def run_search(query, max_results, find_email):
    """Run the scraper as a separate process and stream its progress to the page."""
    cmd = [sys.executable, str(SCRAPER), query, "--max", str(max_results), "--out", str(RESULTS)]
    if not find_email:
        cmd.append("--no-email")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

    st.info(
        "A Chrome window will open and start browsing Google Maps. Please leave it alone. "
        "If Google shows a CAPTCHA or cookie page, handle it in that window and the search continues."
    )
    bar = st.progress(0.0, text="Starting...")
    log_box = st.empty()
    lines, result_file = [], None

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(HERE),
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("RESULT_FILE="):
            result_file = line.split("=", 1)[1]
            continue
        lines.append(line)
        m = re.match(r"\s*(\d+)/(\d+)", line)
        if m:
            bar.progress(int(m[1]) / int(m[2]), text=f"Reading company {m[1]} of {m[2]}")
        log_box.code("\n".join(lines[-12:]))
    proc.wait()

    if result_file and Path(result_file).exists():
        bar.progress(1.0, text="Done")
        st.session_state["last_result"] = result_file
        st.session_state["last_error"] = None
    else:
        bar.empty()
        st.session_state["last_result"] = None
        st.session_state["last_error"] = "\n".join(lines[-25:]) or "The search did not return any data."


def show_result(path):
    df = pd.read_excel(path, dtype=str).fillna("")
    st.subheader("Results")

    c1, c2, c3 = st.columns(3)
    c1.metric("Companies found", len(df))
    c2.metric("With phone", int((df["Phone"] != "").sum()))
    if "Email" in df.columns:
        c3.metric("With email", int((df["Email"] != "").sum()))

    st.dataframe(
        df,
        hide_index=True,
        column_config={
            "Website": st.column_config.LinkColumn("Website"),
            "Google Maps link": st.column_config.LinkColumn("Maps", display_text="Open"),
        },
    )
    if "Reviews" in df.columns:
        small = df[[(v.replace(",", "").strip() == "") or int(re.sub(r"\D", "", v) or 0) < 100 for v in df["Reviews"]]]
        buf = io.BytesIO()
        small.to_excel(buf, index=False)
        st.download_button(f"Download only the {len(small)} companies with fewer than 100 reviews", data=buf.getvalue(),
                           file_name=Path(path).stem + "_under100reviews.xlsx", mime=XLSX_MIME, key="dl_small")
    st.download_button(
        "Download Excel file",
        data=Path(path).read_bytes(),
        file_name=Path(path).name,
        mime=XLSX_MIME,
        type="primary",
    )


st.title("Business Lead Finder")
st.caption("Type a service and a city. You get the companies' contact details as an Excel file.")

with st.form("search"):
    col1, col2 = st.columns(2)
    service = col1.text_input("Service", placeholder="e.g. roofing companies")
    place = col2.text_input("City or area", placeholder="e.g. Rome, Italy")
    col3, col4 = st.columns([2, 1])
    max_results = col3.slider("Number of companies", 5, 200, 30, step=5)
    find_email = col4.checkbox("Also look for emails (slower)", value=True)
    go = st.form_submit_button("Start search", type="primary")

if go:
    if not service.strip() or not place.strip():
        st.error("Please fill in both the service and the city.")
    else:
        run_search(f"{service.strip()} in {place.strip()}", max_results, find_email)

if st.session_state.get("last_error"):
    st.error("The search did not finish. Details:")
    st.code(st.session_state["last_error"])

last = st.session_state.get("last_result")
if last and Path(last).exists():
    show_result(last)

old_files = sorted(RESULTS.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
if old_files:
    with st.expander("Previous searches on this laptop"):
        for f in old_files[:15]:
            when = time.strftime("%d %b %H:%M", time.localtime(f.stat().st_mtime))
            st.download_button(
                f"{f.stem}   ({when})",
                data=f.read_bytes(),
                file_name=f.name,
                mime=XLSX_MIME,
                key=f"old_{f.name}",
            )