"""Audit & email page: audit companies that have an email, review the drafts, approve and send."""

import json
import os
import re
import smtplib
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

import audit
import infographic
import outreach
import report

HERE = Path(__file__).parent
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)
SETTINGS = HERE / "settings.json"
SENT_LOG = RESULTS / "sent_log.csv"

# Gmail by default. The environment overrides exist only so the page can be tested without sending real mail.
SMTP_HOST = os.environ.get("LEADS_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("LEADS_SMTP_PORT", "465"))
SMTP_SSL = os.environ.get("LEADS_SMTP_SSL", "1") == "1"


def friendly_error(e):
    msg = str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError) or "Username and Password not accepted" in msg or "535" in msg:
        return "Gmail rejected the login. Check the address and use an app password, not your normal password."
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address."
    return msg

st.title("Audit and email")
st.caption("Audits every company that has an email, drafts a short email about its biggest problems, and sends it "
           "only after you approve it.")

# ------------------------------------------------------------------ 1. details
cfg = outreach.load_settings(SETTINGS)
with st.expander("1. Your details and Gmail", expanded=not cfg.get("sender_name")):
    c1, c2 = st.columns(2)
    sender_name = c1.text_input("Your name", cfg.get("sender_name", ""), key="cfg_name")
    business = c2.text_input("Your business name", cfg.get("business", ""), key="cfg_business")
    site = c1.text_input("Your website", cfg.get("website", ""), key="cfg_site")
    address = c2.text_input("Your business address (shown in the email footer, recommended)",
                            cfg.get("address", ""), key="cfg_addr")
    gmail = c1.text_input("Your Gmail address", cfg.get("gmail", ""), key="cfg_gmail")
    app_pw = c2.text_input("Gmail app password", type="password", key="cfg_pwd",
                           help="Not your normal password. It is only kept while this page is open.")
    st.caption("The Gmail fields are optional. Leave the password empty and use **Open in Gmail (web)** in step 4 to "
               "send from Gmail itself, so no password is ever typed into this app.")
    city_terms = st.text_input(
        "City / area names used in the local keyword check", cfg.get("city_terms", ""), key="cfg_city",
        help="Comma separated. Add local spellings, e.g. Rome, Roma. If a site uses a spelling you left out, "
             "it will be wrongly flagged.")
    proof = st.text_area(
        "Optional proof sentence (leave empty to skip)",
        "" if cfg.get("proof", "").strip() == outreach.OLD_PROOF.strip() else cfg.get("proof", ""),
        key="cfg_proof", height=80,
        help="Empty by default: the email is written from each company's own audit. Only add a result that is true "
             "for every business you email. Placeholders: {we}, {business}, {category}.")
    cc1, cc2 = st.columns(2)
    def_cc = cc1.text_input("Default Cc (optional, added to every draft)", cfg.get("cc", ""), key="cfg_cc",
                            help="Comma separated. Everyone here can see the Cc address.")
    def_bcc = cc2.text_input("Default Bcc (optional, hidden copy)", cfg.get("bcc", ""), key="cfg_bcc",
                             help="Comma separated. The company does not see Bcc addresses. Handy for a copy to yourself or your team.")
    daily = st.number_input("Maximum emails per day", 1, 100, int(cfg.get("daily", 30)), key="cfg_daily",
                            help="Keep it low. Sending many cold emails from one Gmail account can get it flagged.")
    if st.button("Save details", key="save_cfg"):
        outreach.save_settings(SETTINGS, {"sender_name": sender_name, "business": business, "website": site,
                                          "address": address, "gmail": gmail, "city_terms": city_terms,
                                          "proof": proof, "daily": int(daily), "cc": def_cc, "bcc": def_bcc})
        st.success("Saved. The app password is never saved.")
    with st.expander("How to get a Gmail app password (free)"):
        st.markdown(
            "1. Open your Google Account, then **Security**.\n"
            "2. Turn on **2-Step Verification** if it is off.\n"
            "3. Search for **App passwords**, create one named *Lead Finder*, and copy the 16 characters.\n"
            "4. Paste them above. You will need to paste them again each time you open the app.")

settings = {"sender_name": sender_name, "business": business, "website": site, "address": address,
            "city_terms": city_terms, "proof": proof}

# ------------------------------------------------------------------- 2. audit
st.subheader("2. Audit")
files = sorted((p for p in RESULTS.glob("*.xlsx") if not p.name.startswith("audit_")),
               key=lambda p: p.stat().st_mtime, reverse=True)
if not files:
    st.info("No search results yet. Run a search on the **Find companies** page first.")
    st.stop()

src = st.selectbox("Search results to audit", files, format_func=lambda p: p.stem, key="src_pick")
df = pd.read_excel(src, dtype=str).fillna("")
if "Email" not in df.columns:
    st.warning("This file has no Email column. Run the search again with emails switched on.")
    st.stop()
# The key comes from the ORIGINAL Email column (so old drafts stay valid). Companies without an email get a
# short id instead, so they are still audited. Emails found later or typed by hand live in emails_<search>.json.
df["_key"] = [audit.company_key(r) for r in df.to_dict("records")]
EMAILS_PATH = RESULTS / f"emails_{src.stem}.json"
try:
    SIDE = json.loads(EMAILS_PATH.read_text(encoding="utf-8"))
except Exception:
    SIDE = {}
df["Email"] = [", ".join(audit.split_emails(r["Email"]) + [e for e in audit.split_emails(SIDE.get(r["_key"], ""))
                                                             if e not in audit.split_emails(r["Email"])])
               for r in df.to_dict("records")]
df_a = df.drop_duplicates("_key")
max_rev = st.number_input("Only keep companies with fewer than this many Google reviews (0 = keep all)", 0, 100000, 100,
                          step=10, key="max_reviews",
                          help="Small listings are the best prospects. Rankings and the top-3 competitor names in the "
                               "emails still come from the full list, so nothing is lost.")
if max_rev:
    n_all = len(df_a)
    df_a = df_a[[(audit._to_int(v) is None) or audit._to_int(v) < max_rev for v in df_a["Reviews"]]]
    st.caption(f"{len(df_a)} of {n_all} companies have fewer than {int(max_rev)} reviews (or no review count) "
               f"and are kept. The rest are skipped.")
n_mail = int((df_a["Email"].str.strip() != "").sum())
st.write(f"**{len(df_a)}** companies in this file. **{n_mail}** have an email address and **{len(df_a) - n_mail}** do not. "
         f"All of them are audited, a company without an email just waits until you add one.")

SIDE_KEYS = {k_ for k_ in SIDE if k_ != "_tried"}
if n_mail < len(df_a):
    with st.expander(f"Companies without an email ({len(df_a) - n_mail}): find or type the emails"):
        noem = df_a[df_a["Email"].str.strip() == ""]
        if st.button("Try to find the missing emails from their websites", key="find_missing"):
            import maps_scraper
            todo = [r for r in noem.to_dict("records") if r["Website"].strip()]
            bar2 = st.progress(0.0, text="Looking...")
            hit = 0
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = {pool.submit(maps_scraper.find_emails, r["Website"]): r["_key"] for r in todo}
                for n, fut in enumerate(as_completed(futs), 1):
                    try:
                        got = fut.result()
                    except Exception:
                        got = ""
                    if got:
                        SIDE[futs[fut]] = got
                        hit += 1
                    bar2.progress(n / max(1, len(todo)), text=f"Checked {n} of {len(todo)}")
            EMAILS_PATH.write_text(json.dumps(SIDE, ensure_ascii=False), encoding="utf-8")
            st.session_state["flash_top"] = ("ok", f"Found emails for {hit} of {len(todo)} companies with a website "
                                               f"({len(noem) - len(todo)} have no website to check).")
            st.rerun()
        st.caption("Only emails that are really written on the company's site are used, nothing is guessed. For the "
                   "rest, type the address below (several allowed, separated by commas) and press Save.")
        ed = noem[["Name", "Website", "Phone", "_key"]].set_index("_key").assign(Email="")
        edited = st.data_editor(ed, disabled=["Name", "Website", "Phone"], key=f"ed_missing_{src.stem}",
                                column_config={"Email": st.column_config.TextColumn("Email (type here)")})
        if st.button("Save typed emails", key="save_missing"):
            bad, saved = [], 0
            for k_, v_ in edited["Email"].items():
                v_ = (v_ or "").strip()
                if not v_:
                    continue
                if outreach.bad_addrs(v_):
                    bad.append(f"{ed.loc[k_, 'Name']}: {', '.join(outreach.bad_addrs(v_))}")
                    continue
                SIDE[k_] = v_
                saved += 1
            if bad:
                st.error("These do not look like email addresses: " + "; ".join(bad))
            if saved:
                EMAILS_PATH.write_text(json.dumps(SIDE, ensure_ascii=False), encoding="utf-8")
                st.session_state["flash_top"] = ("ok", f"Saved {saved} email address(es).")
                st.rerun()

flash_ = st.session_state.pop("flash_top", None)
if flash_:
    (st.success if flash_[0] == "ok" else st.error)(flash_[1])

# Everything the PDF needs about the other businesses found in the same Maps search
try:
    META = json.loads(src.with_suffix(".meta.json").read_text(encoding="utf-8"))
except Exception:
    META = {}
QUERY = outreach.clean_query(META.get("query") or src.stem)
COMPS = [{"name": r.get("Name", ""), "rating": audit._to_float(r.get("Rating")), "reviews": audit._to_int(r.get("Reviews")),
          "category": r.get("Category", ""), "website": r.get("Website", ""), "rank": i,
          "description": r.get("Description", ""), "claimed": r.get("Claimed", ""), "hours": r.get("Hours", "")}
         for i, r in enumerate(df.to_dict("records"), 1)]
RANK = {}
for i, key in enumerate(df["_key"], 1):
    if key and key not in RANK:
        RANK[key] = i

TOP3 = [c["name"] for c in COMPS[:3]]


def email_ctx(a_):
    return {"query": QUERY, "rank": RANK.get(a_["key"]), "top": TOP3, "comps": COMPS}


EXTRA_PATH = RESULTS / f"extra_{src.stem}.json"
DRAFT_PATH = RESULTS / f"drafts_{src.stem}.json"
try:
    EXTRAS = json.loads(EXTRA_PATH.read_text(encoding="utf-8"))
except Exception:
    EXTRAS = {}


def make_pdf(a_):
    """The full report (several pages) for download."""
    return report.build_pdf(a_, EXTRAS.get(a_["key"], {}), COMPS, settings, QUERY,
                            RANK.get(a_["key"]), len(df))


def image_for(a_):
    """The one-image report (JPG) that goes under the email text. Built once, then kept until the details change."""
    k_ = a_["key"]
    key = (src.name, k_, json.dumps(EXTRAS.get(k_, {}), sort_keys=True), settings.get("business", ""))
    cache = st.session_state.setdefault("img_cache", {})
    if key not in cache:
        while len(cache) >= 30:
            cache.pop(next(iter(cache)))
        args = (a_, EXTRAS.get(k_, {}), COMPS, settings, QUERY, RANK.get(k_), len(df))
        try:
            cache[key] = infographic.build_infographic(*args)
            st.session_state.pop("img_warn", None)
        except Exception as e:  # e.g. Chromium missing: fall back to the plain pad report so sending still works
            st.session_state["img_warn"] = f"Infographic failed ({e}). Using the plain pad report instead."
            cache[key] = report.build_image(*args)
    return cache[key]

saved_path = RESULTS / f"audit_{src.stem}.json"
b1, b2 = st.columns([1, 1])
run = b1.button("Run audit", type="primary", disabled=df_a.empty, key="run_audit")
if saved_path.exists():
    stamp = time.strftime("%d %b %H:%M", time.localtime(saved_path.stat().st_mtime))
    if b2.button(f"Load previous audit ({stamp})", key="load_audit"):
        data = json.loads(saved_path.read_text(encoding="utf-8"))
        st.session_state["audits"], st.session_state["audit_src"] = data["audits"], src.name

if run:
    terms = [t.strip() for t in ([outreach.city_from(QUERY)] + city_terms.split(",")) if t.strip()]
    terms = list(dict.fromkeys(terms))
    rows = df_a.to_dict("records")
    tried = set(SIDE.get("_tried", []))
    todo_ = [r for r in rows if not r["Email"].strip() and r["Website"].strip() and r["_key"] not in tried]
    if todo_:
        import maps_scraper
        bar0 = st.progress(0.0, text="Looking for missing emails on their websites...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs0 = {pool.submit(maps_scraper.find_emails, r["Website"]): r for r in todo_}
            for n, fut in enumerate(as_completed(futs0), 1):
                r = futs0[fut]
                try:
                    got = fut.result()
                except Exception:
                    got = ""
                tried.add(r["_key"])
                if got:
                    SIDE[r["_key"]] = got
                    r["Email"] = got
                bar0.progress(n / len(todo_), text=f"Checked website {n} of {len(todo_)}")
        SIDE["_tried"] = sorted(tried)
        EMAILS_PATH.write_text(json.dumps(SIDE, ensure_ascii=False), encoding="utf-8")
    bar = st.progress(0.0, text="Auditing...")
    results = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(audit.audit_company, r, terms): i for i, r in enumerate(rows)}
        for n, fut in enumerate(as_completed(futures), 1):
            results[futures[fut]] = fut.result()
            bar.progress(n / len(rows), text=f"Audited {n} of {len(rows)}")
    saved_path.write_text(json.dumps({"audits": results}, ensure_ascii=False), encoding="utf-8")
    # a fresh audit means fresh drafts: forget old drafts, edits and approvals
    DRAFT_PATH.unlink(missing_ok=True)
    for stale in [n for n in st.session_state if str(n).startswith(("subj_", "body_", "ok_", "to_", "cc_", "bcc_", "pick_", "drafts"))]:
        st.session_state.pop(stale, None)
    st.session_state["audits"], st.session_state["audit_src"] = results, src.name
    bar.progress(1.0, text="Audit finished")

audits = st.session_state.get("audits") if st.session_state.get("audit_src") == src.name else None
if not audits:
    st.stop()

# Every audit gets a stable key, and its emails always come from the current file (so an address you add
# later shows up without running the audit again). Old saved audits have no key: the email was the key.
EM = {r["_key"]: audit.split_emails(r["Email"]) for r in df_a.to_dict("records")}
for a_ in audits:
    a_["key"] = a_.get("key") or (a_.get("email") or "").lower()
    a_["emails"] = EM.get(a_["key"]) or audit.split_emails(a_.get("email"))
    a_["email"] = a_["emails"][0] if a_["emails"] else ""

# --------------------------------------------------------------- 3. overview
sent_set = outreach.sent_emails(SENT_LOG)

# Drafts, edits and approvals live here (and on disk), not in widget state, so they survive switching companies.
if st.session_state.get("drafts_src") != src.name:
    try:
        st.session_state["drafts"] = json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
    except Exception:
        st.session_state["drafts"] = {}
    st.session_state["drafts_src"] = src.name
    st.session_state.pop("drafts_blob", None)
DRAFTS = st.session_state["drafts"]


def draft_for(a_):
    k_ = a_["key"]
    if k_ not in DRAFTS:
        subj_, body_ = outreach.build_email(a_, settings, email_ctx(a_))
        DRAFTS[k_] = {"subject": subj_, "body": body_, "to": a_["email"], "approved": False,
                      "cc": def_cc, "bcc": def_bcc}
    return DRAFTS[k_]


def save_drafts():
    blob = json.dumps(DRAFTS, ensure_ascii=False)
    if st.session_state.get("drafts_blob") != blob:
        DRAFT_PATH.write_text(blob, encoding="utf-8")
        st.session_state["drafts_blob"] = blob


def was_sent(a):
    return a["key"] in sent_set or any(e in sent_set for e in a["emails"])


def status_of(a):
    k_ = a["key"]
    if was_sent(a):
        return "Sent"
    if a["problems"]:
        if not a["emails"] and not outreach.EMAIL_RE.match(DRAFTS.get(k_, {}).get("to", "").strip()):
            return "Needs an email"
        return "Approved, waiting to send" if DRAFTS.get(k_, {}).get("approved") else "Ready to review"
    return "Website could not be checked" if a["page_status"] != "ok" else "No serious problem found"


def label_of(cid):
    return audit.CHECKS[cid][0]


st.subheader("3. Results")
m1, m2, m3 = st.columns(3)
m1.metric("Companies audited", len(audits))
m2.metric("Emails to send", sum(1 for a in audits if status_of(a) in ("Ready to review", "Approved, waiting to send")))
m3.metric("Sent", sum(1 for a in audits if status_of(a) == "Sent"))

overview = pd.DataFrame([{
    "Company": a["company"], "Score": a["score"],
    "Biggest problems": ", ".join(label_of(p) for p in a["problems"]) or "-",
    "Status": status_of(a),
} for a in audits])
st.dataframe(overview, hide_index=True)


def build_report():
    summary, checks, drafts = [], [], []
    for a in audits:
        k = a["key"]
        summary.append({"Company": a["company"], "Email": ", ".join(a["emails"]), "Website": a["website"],
                        "Google Maps": a["maps"], "Score": a["score"],
                        "Biggest problems": ", ".join(label_of(p) for p in a["problems"]),
                        "Website check": a["page_status"], "Status": status_of(a)})
        for c in a["checks"]:
            checks.append({"Company": a["company"], "Area": c["area"], "Check": c["label"],
                           "Result": {True: "OK", False: "Problem", None: "Not checked"}[c["ok"]],
                           "Details": c["detail"]})
        d = outreach.build_email(a, settings, email_ctx(a))
        if d:
            dd = DRAFTS.get(k, {})
            drafts.append({"Company": a["company"], "To": dd.get("to", a["email"]), "Cc": dd.get("cc", ""),
                           "Bcc": dd.get("bcc", ""),
                           "Subject": dd.get("subject", d[0]), "Body": dd.get("body", d[1])})
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame(summary).to_excel(w, sheet_name="Summary", index=False)
        pd.DataFrame(checks).to_excel(w, sheet_name="All checks", index=False)
        pd.DataFrame(drafts).to_excel(w, sheet_name="Email drafts", index=False)
    return buf.getvalue()


d1, d2 = st.columns(2)
d1.download_button("Download summary of all checks (Excel)", data=build_report(),
                   file_name=f"audit_report_{src.stem}.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
if d2.button("Prepare all PDF audit reports (ZIP)", key="make_zip"):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, a_ in enumerate(audits, 1):
            z.writestr(f"{n:02d}_{report.safe_filename(a_['company'])}_audit.pdf", make_pdf(a_))
    st.session_state["zip_bytes"] = (src.name, buf.getvalue())
zb = st.session_state.get("zip_bytes")
if zb and zb[0] == src.name:
    d2.download_button("Download the PDF reports (ZIP)", data=zb[1], file_name=f"audit_pdfs_{src.stem}.zip",
                       mime="application/zip", type="primary")

# ---------------------------------------------------------------- 4. review
st.subheader("4. Review and approve")
ready = [a for a in audits if a["problems"]]
if not ready:
    st.info("None of these companies has a serious problem we could confirm, so no emails were drafted.")
    st.stop()
ready.sort(key=lambda a: (a["score"] is None, a["score"]))

# Take over whatever was just typed or ticked, so the labels below are never one click behind.
for k_, d_ in DRAFTS.items():
    if f"subj_{k_}" in st.session_state:
        d_.update(subject=st.session_state[f"subj_{k_}"], body=st.session_state.get(f"body_{k_}", d_["body"]),
                  approved=bool(st.session_state.get(f"ok_{k_}", d_["approved"])),
                  to=st.session_state.get(f"to_{k_}", d_["to"]),
                  cc=st.session_state.get(f"cc_{k_}", d_.get("cc", "")),
                  bcc=st.session_state.get(f"bcc_{k_}", d_.get("bcc", "")))

idx = st.selectbox("Company", range(len(ready)), key="review_pick",
                   format_func=lambda i: f"{ready[i]['company']}  |  score {ready[i]['score']}  |  {status_of(ready[i])}")
a = ready[idx]
k = a["key"]
d = draft_for(a)
skey, bkey, okey, tkey = f"subj_{k}", f"body_{k}", f"ok_{k}", f"to_{k}"
ckey, xkey, pkey = f"cc_{k}", f"bcc_{k}", f"pick_{k}"
# Widget state disappears when a company is not on screen, so restore it from the saved draft.
for key_, val_ in ((skey, d["subject"]), (bkey, d["body"]), (okey, d["approved"]), (tkey, d["to"]),
                   (ckey, d.get("cc", "")), (xkey, d.get("bcc", ""))):
    if key_ not in st.session_state:
        st.session_state[key_] = val_
if not st.session_state[tkey].strip() and a["emails"]:  # an address was added after the draft was made
    st.session_state[tkey] = a["emails"][0]


def reset_draft(key_, a_):
    subj_, body_ = outreach.build_email(a_, settings, email_ctx(a_))
    for name_, val_ in ((f"subj_{key_}", subj_), (f"body_{key_}", body_), (f"to_{key_}", a_["email"]),
                        (f"ok_{key_}", False), (f"cc_{key_}", def_cc), (f"bcc_{key_}", def_bcc)):
        st.session_state[name_] = val_
    st.session_state.pop(f"pick_{key_}", None)


def pick_to(tkey_, pkey_):
    """Dropdown callback: copy the chosen address into the To box."""
    v_ = st.session_state.get(pkey_)
    if v_:
        st.session_state[tkey_] = v_


def send_now(a_):
    """Runs the moment the Send button is pressed (before the page redraws), so one tap always sends."""
    k_ = a_["key"]
    to_ = st.session_state[f"to_{k_}"].strip()
    cc_, bcc_ = st.session_state[f"cc_{k_}"].strip(), st.session_state[f"bcc_{k_}"].strip()
    test_ = bool(a_["emails"]) and to_.lower() not in a_["emails"]
    subj_, body_ = st.session_state[f"subj_{k_}"], st.session_state[f"body_{k_}"]
    try:
        img_ = image_for(a_) if st.session_state.get("attach_img", True) else None
        outreach.send_email(to_, ("[TEST] " if test_ else "") + subj_, body_, sender_name, gmail, app_pw,
                            SMTP_HOST, SMTP_PORT, SMTP_SSL, image=img_,
                            cc=None if test_ else cc_, bcc=None if test_ else bcc_)
        if test_:
            st.session_state["flash"] = ("ok", f"Test email sent to {to_}. Check that inbox (and its spam folder).")
        else:
            outreach.log_sent(SENT_LOG, to_, a_["company"], subj_)
            st.session_state[f"ok_{k_}"] = True
            st.session_state["flash"] = ("ok", f"Sent to {to_}.")
    except Exception as e:
        st.session_state["flash"] = ("err", f"Could not send: {friendly_error(e)}")


flash = st.session_state.pop("flash", None)
if flash:
    (st.success if flash[0] == "ok" else st.error)(flash[1])

left, right = st.columns([5, 6])
with left:
    st.markdown("**Audit report**")
    st.metric("Score", f"{a['score']} / 100" if a["score"] is not None else "n/a")
    st.markdown(f"[Website]({a['website']})  ·  [Google Maps listing]({a['maps']})")
    if a["page_status"] != "ok":
        st.warning(a["page_status"] + ". Website checks were skipped.")
    img_bytes = None
    try:
        with st.spinner("Building the report image..."):
            img_bytes = image_for(a)
        if st.session_state.get("img_warn"):
            st.warning(st.session_state["img_warn"])
        st.image(img_bytes, caption="This image goes under the email text")
        st.download_button("Download report image (JPG)", data=img_bytes, key=f"jpg_{k}",
                           file_name=f"{report.safe_filename(a['company'])}_audit.jpg", mime="image/jpeg")
    except Exception as e:
        st.error(f"Could not build the report image: {e}")
    try:
        st.download_button("Download full report (PDF)", data=make_pdf(a), type="primary",
                           file_name=f"{report.safe_filename(a['company'])}_audit.pdf", mime="application/pdf",
                           key=f"pdf_{k}")
    except Exception as e:
        st.error(f"Could not build the PDF: {e}")

    ex = {**(a.get("gbp") or {}), **EXTRAS.get(k, {})}
    yn_opts = ["Unknown", "Yes", "No"]
    with st.expander("Add details from GMB Everywhere (optional, makes the PDF more complete)"):
        st.caption("Leave a field empty if you don't know it. Unknown items are left out of the report and the "
                   "scores, and listed in a note.")
        with st.form(f"extra_form_{k}"):
            f1, f2 = st.columns(2)
            claimed = f1.selectbox("Profile claimed?", yn_opts, index=yn_opts.index(ex.get("claimed") or "Unknown"))
            hours = f2.selectbox("Opening hours listed?", yn_opts, index=yn_opts.index(ex.get("hours") or "Unknown"))
            desc = f1.selectbox("Description filled in?", yn_opts, index=yn_opts.index(ex.get("description") or "Unknown"))
            sec = f2.text_input("Secondary categories (number)", "" if ex.get("secondary_categories") is None else str(ex["secondary_categories"]))
            photos = f1.text_input("Photos (number)", "" if ex.get("photos") is None else str(ex["photos"]))
            services = f2.text_input("Services listed (number)", "" if ex.get("services") is None else str(ex["services"]))
            lp_old = ex.get("last_post_months")
            last_post = f1.text_input("Last post (months ago; 0 = this month; or 'never')",
                                      "" if lp_old is None else "never" if lp_old >= 99 else str(lp_old))
            dirs = f2.text_input("Core directories confirmed (0 to 8)", "" if ex.get("directories") is None else str(ex["directories"]))
            if st.form_submit_button("Save details"):
                def num(s, hi=None):
                    s = (s or "").strip()
                    if not s.isdigit():
                        return None
                    return min(int(s), hi) if hi else int(s)
                new = {"claimed": "" if claimed == "Unknown" else claimed, "hours": "" if hours == "Unknown" else hours,
                       "description": "" if desc == "Unknown" else desc, "secondary_categories": num(sec),
                       "photos": num(photos), "services": num(services),
                       "last_post_months": 99 if last_post.strip().lower() == "never" else num(last_post),
                       "directories": num(dirs, 8)}
                EXTRAS[k] = new
                EXTRA_PATH.write_text(json.dumps(EXTRAS, ensure_ascii=False), encoding="utf-8")
                st.rerun()
    st.dataframe(pd.DataFrame([{
        "Area": c["area"], "Check": c["label"],
        "Result": {True: "OK", False: "Problem", None: "Not checked"}[c["ok"]],
        "Details": c["detail"],
    } for c in a["checks"]]), hide_index=True)
    st.caption("Homepage only. Speed is not measured, because it would depend on where this laptop is. "
               "Problems marked with severity 5 or more can appear in the email (the top two are used).")

with right:
    st.markdown("**Email draft**")
    if len(a["emails"]) > 1:
        st.selectbox(f"This company has {len(a['emails'])} emails. Pick one for 'To'", a["emails"], index=None,
                     placeholder="Choose an address...", key=pkey, on_change=pick_to, args=(tkey, pkey))
    st.text_input("To (type your own address here to test)" if a["emails"] else
                  "To (this company has no email yet: type one, or add it in step 2)", key=tkey)
    cc_col, bcc_col = st.columns(2)
    cc_col.text_input("Cc (optional)", key=ckey, help="Comma separated. Visible to the company.")
    bcc_col.text_input("Bcc (optional)", key=xkey, help="Comma separated. Hidden from the company.")
    st.text_input("Subject", key=skey)
    st.text_area("Body", key=bkey, height=430)
    st.button("Reset draft", key=f"reset_{k}", on_click=reset_draft, args=(k, a))
    attach = st.checkbox("Show the audit report image under the text of the email", value=True, key="attach_img",
                         help="The JPG on the left is added inline, below your text. Applies to Gmail sending from this app, "
                              "the test copies and the batch. The Gmail web and mail-app buttons cannot carry an image: "
                              "download the JPG and paste it in.")
    use_img = img_bytes if attach else None
    flagged = outreach.spam_words(st.session_state[skey] + " " + st.session_state[bkey])
    if flagged:
        st.warning("Words that often trigger spam filters: " + ", ".join(flagged) + ". Consider rewording.")
    if outreach.has_link(st.session_state[skey] + " " + st.session_state[bkey]):
        st.warning("This email contains a link or web address. Cold emails with links are filtered more, so leave "
                   "it out of the first email.")

    to_addr = st.session_state[tkey].strip()
    cc_addr, bcc_addr = st.session_state[ckey].strip(), st.session_state[xkey].strip()
    # A test is when 'To' is an address that does not belong to the company. A company with no emails of its
    # own has nothing to compare with, so whatever valid address you type there is a real recipient.
    is_test = bool(a["emails"]) and to_addr.lower() not in a["emails"]
    already = was_sent(a) or to_addr.lower() in sent_set
    problems = []
    if not outreach.EMAIL_RE.match(to_addr):
        problems.append("The 'To' address looks invalid." if to_addr else "Add an email address in 'To' first.")
    for label_, val_ in (("Cc", cc_addr), ("Bcc", bcc_addr)):
        if outreach.bad_addrs(val_):
            problems.append(f"{label_} has an invalid address: {', '.join(outreach.bad_addrs(val_))}.")
    if not (sender_name and business):
        problems.append("Fill in your name and business name in step 1.")
    if not is_test and outreach.sent_today(SENT_LOG) >= int(daily):
        problems.append(f"Daily limit of {int(daily)} emails reached.")
    creds = bool(gmail and app_pw)

    if already and not is_test:
        st.success("This email has already been sent.")
    else:
        for p in problems:
            st.warning(p)
        if is_test:
            st.info("You changed the address, so this counts as a **test**: the subject gets a [TEST] prefix, it is not "
                    "marked as sent, and it does not use up the daily limit. Press Reset draft to go back to the "
                    "company's own address.")
        approved = st.checkbox("Approve for the batch in step 5 (not needed for the Send button below)", key=okey,
                               help="Approved emails are collected in step 5 so you can send them all at once.")
        blocked = bool(problems)
        s1, s2 = st.columns(2)
        s1.button(f"Send test to {to_addr}" if is_test else "Send with Gmail now", type="primary",
                  key=f"send_{k}", disabled=blocked or not creds, on_click=send_now, args=(a,))
        test_it = s2.button("Send a test copy to myself", key=f"test_{k}", disabled=not creds)
        if not creds:
            st.caption("No Gmail details entered, so sending from this app is off. Use **Open in Gmail (web)** below "
                       "instead: you press Send in Gmail yourself, then click \"I sent it myself\". No password is "
                       "typed into this app.")
        cc_q = f"&cc={quote(','.join(outreach.split_addrs(cc_addr)))}" if cc_addr and not is_test else ""
        bcc_q = f"&bcc={quote(','.join(outreach.split_addrs(bcc_addr)))}" if bcc_addr and not is_test else ""
        mailto = (f"mailto:{to_addr}?subject={quote(st.session_state[skey])}{cc_q}{bcc_q}"
                  f"&body={quote(st.session_state[bkey].replace(chr(10), chr(13) + chr(10)))}")
        gmail_url = ("https://mail.google.com/mail/?view=cm&fs=1"
                     + (f"&authuser={quote(gmail)}" if gmail else "")
                     + f"&to={quote(to_addr)}{cc_q}{bcc_q}&su={quote(st.session_state[skey])}&body={quote(st.session_state[bkey])}")
        m1, m2 = st.columns(2)
        m1.link_button("Open in Gmail (web)", gmail_url, disabled=blocked,
                       help="Opens a Gmail compose window in your browser with everything filled in. Check it and press Send there.")
        mark_it = m2.button("I sent it myself (mark as sent)", key=f"mark_{k}", disabled=blocked or is_test,
                            help="Records it in the sent log so this company is not emailed twice.")
        st.link_button("Or open in my default email app (Outlook etc.)", mailto, disabled=blocked)
        if attach:
            st.caption("Gmail web and mail-app links cannot include the image. Download the JPG on the left and paste it "
                       "into the body, or send from this app with Gmail details.")
        if len(gmail_url) > 6000:
            st.caption("This email is very long. If Gmail cuts it off, copy the body from the box above instead.")
        if mark_it:
            outreach.log_sent(SENT_LOG, to_addr, a["company"], st.session_state[skey])
            st.session_state["flash"] = ("ok", f"Marked as sent: {to_addr}.")
            st.rerun()
        try:
            if test_it:
                outreach.send_email(gmail, "[TEST] " + st.session_state[skey], st.session_state[bkey], sender_name,
                                    gmail, app_pw, SMTP_HOST, SMTP_PORT, SMTP_SSL, image=use_img)
                st.success(f"Test copy sent to {gmail}.")
        except Exception as e:
            st.error(f"Could not send: {friendly_error(e)}")

# keep the saved draft in step with what is on screen
d.update(subject=st.session_state[skey], body=st.session_state[bkey], approved=bool(st.session_state[okey]),
         to=st.session_state[tkey], cc=st.session_state[ckey], bcc=st.session_state[xkey])
save_drafts()

# ------------------------------------------------------------- 5. send all
st.subheader("5. Send everything you approved")
res = st.session_state.pop("bulk_result", None)
if res:
    if res["ok"]:
        st.success(f"{res['label']}: {len(res['ok'])} email(s) " + ("sent" if not res["test"] else "sent as tests") + ".")
    for company_, why_ in res["failed"]:
        st.error(f"{company_}: {why_}")

sent_now = outreach.sent_emails(SENT_LOG)
def real_to(x):
    """The draft's 'To' when it is a real recipient (one of the company's emails, or any valid typed address
    for a company that has none); otherwise '' (it is a test address)."""
    t = DRAFTS.get(x["key"], {}).get("to", "").strip()
    if not outreach.EMAIL_RE.match(t) or t.lower() in sent_now:
        return ""
    return t if (t.lower() in x["emails"] or not x["emails"]) else ""


approved_all = [x for x in ready if DRAFTS.get(x["key"], {}).get("approved") and not was_sent(x)]
queue = [x for x in approved_all if real_to(x)]
changed = len(approved_all) - len(queue)
left_today = max(0, int(daily) - outreach.sent_today(SENT_LOG))
st.write(f"**{len(queue)}** approved and waiting  ·  **{len(sent_now)}** sent so far  ·  you can send "
         f"**{left_today}** more today (daily limit {int(daily)}).")
if changed:
    st.caption(f"{changed} approved draft(s) have a changed 'To' address. Those are treated as tests and are never "
               "part of this batch.")
if queue:
    st.dataframe(pd.DataFrame([{"Company": x["company"], "To": real_to(x), "Cc": DRAFTS[x["key"]].get("cc", ""),
                               "Bcc": DRAFTS[x["key"]].get("bcc", ""), "Subject": DRAFTS[x["key"]]["subject"]}
                               for x in queue]), hide_index=True)
    st.download_button("Download approved emails (CSV, for a mail merge)", key="dl_queue", mime="text/csv",
                       data=pd.DataFrame([{"To": real_to(x), "Cc": DRAFTS[x["key"]].get("cc", ""),
                                           "Bcc": DRAFTS[x["key"]].get("bcc", ""), "Subject": DRAFTS[x["key"]]["subject"],
                                           "Body": DRAFTS[x["key"]]["body"]} for x in queue]
                                         ).to_csv(index=False).encode("utf-8-sig"),
                       file_name="approved_emails.csv")
else:
    st.caption("Nothing is waiting. Open each company above, read its email and tick the approval box. "
               "Approved emails collect here.")


def run_batch(items, label, test_to=None, gap_s=0):
    ok_, failed_ = [], []
    bar = st.progress(0.0, text="Starting...")
    for n, x in enumerate(items, 1):
        dx = draft_for(x)
        to_x = real_to(x)
        if test_to is None:
            if not to_x or was_sent(x) or to_x.lower() in outreach.sent_emails(SENT_LOG):
                continue
            if outreach.sent_today(SENT_LOG) >= int(daily):
                failed_.append((x["company"], "Daily limit reached, the rest stay in the queue."))
                break
        bar.progress((n - 1) / len(items), text=f"Sending {n} of {len(items)}: {x['company']}")
        try:
            outreach.send_email(test_to or to_x, ("[TEST] " if test_to else "") + dx["subject"], dx["body"],
                                sender_name, gmail, app_pw, SMTP_HOST, SMTP_PORT, SMTP_SSL,
                                image=image_for(x) if st.session_state.get("attach_img", True) else None,
                                cc=None if test_to else dx.get("cc"), bcc=None if test_to else dx.get("bcc"))
            if test_to is None:
                outreach.log_sent(SENT_LOG, to_x, x["company"], dx["subject"])
            ok_.append(x["company"])
        except Exception as e:
            failed_.append((x["company"], friendly_error(e)))
            if isinstance(e, smtplib.SMTPAuthenticationError):
                break  # wrong login: every further attempt would fail too
        if n < len(items) and gap_s:
            time.sleep(gap_s)
    st.session_state["bulk_result"] = {"label": label, "ok": ok_, "failed": failed_, "test": test_to is not None}
    st.rerun()


can_send = bool(sender_name and business and gmail and app_pw)
b1, b2 = st.columns(2)
with b1:
    st.markdown("**Send the batch to the companies**")
    gap = st.number_input("Pause between emails (seconds)", 0, 600, 30, key="bulk_gap",
                          help="Keep at least 20 seconds for real sends. A rapid burst can get a Gmail account blocked.")
    n_send = min(len(queue), left_today)
    confirm = st.checkbox(f"Yes, send {n_send} email(s) to these companies now", key="bulk_ok", disabled=n_send == 0)
    go = st.button("Send all approved emails", type="primary", key="bulk_send",
                   disabled=not (n_send and confirm and can_send))
    if not can_send:
        st.caption("Needs your name, business and Gmail details from step 1. Without Gmail, download the CSV above "
                   "or send from your own mail app.")
    if len(queue) > left_today and queue:
        st.caption(f"Only the first {left_today} will go today because of the daily limit.")
    st.caption("Keep this page open while it runs. If you click something, it stops safely: what was already sent is "
               "logged, and pressing the button again continues with the rest.")
with b2:
    st.markdown("**Test the whole batch first**")
    t_addr = st.text_input("Send test copies to", gmail, key="bulk_test_addr")
    t_n = st.number_input("How many drafts to test", 1, len(ready), min(3, len(ready)), key="bulk_test_n")
    test_go = st.button("Send test copies", key="bulk_test",
                        disabled=not (can_send and outreach.EMAIL_RE.match(t_addr.strip())),
                        help="Sends the first drafts, exactly as written, to the address above instead of the "
                             "companies. Nothing is logged as sent.")

if go:
    run_batch(queue[:left_today], "Batch finished", None, int(gap))
if test_go:
    run_batch([draft_item for draft_item in ready[: int(t_n)]], "Test finished", t_addr.strip(), 1)

st.caption("Cold emailing is regulated (for example GDPR and national privacy rules in the EU, CAN-SPAM in the US). "
           "Check what applies where your recipients are. Make sure every email clearly says who you are, and honour anyone who asks you to stop.")