"""Email tracker: see the emails already sent, by category, country and city."""

import json
import os
import re
import smtplib
from pathlib import Path

import pandas as pd
import streamlit as st

import audit
import infographic
import outreach
import report

HERE = Path(__file__).parent
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)
SENT_DIR = RESULTS / "sent_emails"
SENT_LOG = RESULTS / "sent_log.csv"
SETTINGS = HERE / "settings.json"
# Gmail by default (the environment overrides exist only for testing without sending real mail)
SMTP_HOST = os.environ.get("LEADS_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("LEADS_SMTP_PORT", "465"))
SMTP_SSL = os.environ.get("LEADS_SMTP_SSL", "1") == "1"


def nice(text):
    """'bangladesh' -> 'Bangladesh', but short names stay capitals: 'usa' -> 'USA', 'uk' -> 'UK'."""
    t = text.strip()
    return t.upper() if len(t) <= 3 and t.replace(".", "").isalpha() else t.title()


def parse_query(query):
    """'dentist in Dhaka, Bangladesh' -> ('dentist', 'Bangladesh', 'Dhaka').

    The words before the first ' in ' are the category. In the place, the last comma part is the country and the
    first is the city. With no comma the country is 'Unknown'."""
    q = " ".join((query or "").replace("_", " ").split())
    cat, sep, place = q.partition(" in ")
    if not sep:
        return q.lower(), "Unknown", "Unknown"
    parts = [p.strip() for p in place.split(",") if p.strip()]
    city = parts[0].title() if parts else "Unknown"
    country = nice(parts[-1]) if len(parts) > 1 else "Unknown"
    return cat.strip().lower(), country, city


def all_searches():
    """Every search that was ever run (one per results file), read from the files in results/."""
    found = []
    for x in sorted(RESULTS.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True):
        if x.name.startswith("audit_"):
            continue
        try:
            query = json.loads(x.with_suffix(".meta.json").read_text(encoding="utf-8")).get("query", "")
        except Exception:
            query = ""
        cat, country, city = parse_query(query or x.stem)
        found.append({"file": x, "query": query or x.stem, "category": cat, "country": country, "city": city})
    return found


st.title("Email tracker")
st.caption("Look back at the emails you have already sent: the audit report image, the email text and when it was sent.")

_flash = st.session_state.pop("trk_flash", None)
if _flash:
    (st.success if _flash[0] == "ok" else st.error)(_flash[1])

SEARCHES = all_searches()
if not SEARCHES:
    st.info("No searches yet. Run a search on the **Find companies** page and it will appear here.")
    st.stop()

# the list is rebuilt from the files every time, so a new search appears here by itself
categories = sorted({s["category"] for s in SEARCHES})
category = st.selectbox("Category", categories, format_func=lambda c: c.title(), key="trk_category",
                        help="Taken from the words you searched, e.g. 'dentist' from 'dentist in Dhaka, Bangladesh'.")

# only the countries that were searched for this category, e.g. dentist -> Bangladesh, Usa
in_category = [s for s in SEARCHES if s["category"] == category]
countries = sorted({s["country"] for s in in_category})
country = st.selectbox("Country", countries, key=f"trk_country_{category}",
                       help="The countries you searched this category in.")

# only the cities searched for this category and country, e.g. dentist + Bangladesh -> Dhaka, Khulna
in_country = [s for s in in_category if s["country"] == country]
cities = sorted({s["city"] for s in in_country})
city = st.selectbox("City", cities, key=f"trk_city_{category}_{country}",
                    help="The cities you searched for this category in this country.")

# ------------------------------------------------- emails sent before the tracker existed
def older_records(saved):
    """Emails sent before copies were saved. The time and subject come from the sent log; the text comes from
    the saved draft of that company (drafts_<search>.json), which is what was on screen when it was sent."""
    have = {r["to"].lower() for r in saved}
    drafts = {}
    for f in RESULTS.glob("drafts_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key, d in data.items():
            for addr in {(d.get("to") or "").strip().lower(), key.lower()}:
                if addr:
                    drafts.setdefault(addr, (f.stem[len("drafts_"):], d))
    out = []
    for row in outreach.read_log(SENT_LOG):
        to = row["email"].lower()
        if to in have:
            continue
        stem, d = drafts.get(to, ("", {}))
        out.append({"time": row["time"], "search": stem, "company": row["company"], "to": row["email"],
                    "cc": d.get("cc", ""), "bcc": d.get("bcc", ""), "subject": row.get("subject") or d.get("subject", ""),
                    "body": d.get("body", ""), "image": "", "older": True})
    return out


def load_audit(stem, company):
    """(audit, extra details, competitors, rank, total, query) for one company of one search, from the saved files."""
    audits = json.loads((RESULTS / f"audit_{stem}.json").read_text(encoding="utf-8"))["audits"]
    a = next(x for x in audits if x["company"] == company)
    df = pd.read_excel(RESULTS / f"{stem}.xlsx", dtype=str).fillna("")
    keys = [audit.company_key(r) for r in df.to_dict("records")]
    a["key"] = a.get("key") or (a.get("email") or "").lower()
    comps = [{"name": r.get("Name", ""), "rating": audit._to_float(r.get("Rating")),
              "reviews": audit._to_int(r.get("Reviews")), "category": r.get("Category", ""),
              "website": r.get("Website", ""), "rank": i, "description": r.get("Description", ""),
              "claimed": r.get("Claimed", ""), "hours": r.get("Hours", "")}
             for i, r in enumerate(df.to_dict("records"), 1)]
    rank = keys.index(a["key"]) + 1 if a["key"] in keys else None
    try:
        extra = json.loads((RESULTS / f"extra_{stem}.json").read_text(encoding="utf-8")).get(a["key"], {})
    except Exception:
        extra = {}
    try:
        query = json.loads((RESULTS / f"{stem}.meta.json").read_text(encoding="utf-8")).get("query", stem)
    except Exception:
        query = stem
    return a, extra, comps, rank, len(df), outreach.clean_query(query)


def rebuild_image(stem, company):
    """Draw the report image again from the saved audit of this search (the original JPG was not kept)."""
    a, extra, comps, rank, total, query = load_audit(stem, company)
    return infographic.build_infographic(a, extra, comps, outreach.load_settings(SETTINGS), query, rank, total)


def scores_for(r):
    """The report's score areas for this email's company: [(label, percent, band)], best first. [] if unknown."""
    ck = (r.get("search"), r.get("company"))
    cache = st.session_state.setdefault("trk_scores", {})
    if ck not in cache:
        try:
            a, extra, comps, *_ = load_audit(*ck)
            sc = report.compute_scores(a, extra, comps)
            rows = [(infographic.LABEL.get(n, n), int(round(v)), infographic.band_name(v))
                    for n, v, _ in sc["cats"] if v is not None]
            cache[ck] = (sorted(rows, key=lambda t: -t[1]), a)
        except Exception:
            cache[ck] = ([], None)
    return cache[ck]


def pick_topic(fid, r):
    """Dropdown callback: write the follow-up text for the chosen score area."""
    k, ss = f"fu_{fid}", st.session_state
    choice = ss.get(k + "_topic")
    rows, a = scores_for(r)
    cfg = outreach.load_settings(SETTINGS)
    for label, pct, band in rows:
        if choice == f"{label} ({pct}%)":
            ss[k + "_body"] = outreach.followup_body(r["company"], label, pct, band, a, cfg.get("sender_name", ""),
                                                     cfg.get("business", ""))
            return
    ss[k + "_body"] = ss.get(k + "_default", ss.get(k + "_body", ""))


def fid_of(r):
    return re.sub(r"\W+", "_", r.get("file") or (r["time"] + r["to"]))


def toggle_followup(fid, r):
    """Opens the follow-up form with a ready-made reply text (only the first time)."""
    k = f"fu_{fid}"
    cfg = outreach.load_settings(SETTINGS)
    if not st.session_state.get(k + "_open"):
        subj = r["subject"] if r["subject"].lower().startswith("re:") else "Re: " + r["subject"]
        st.session_state[k + "_to"] = r["to"]
        st.session_state[k + "_cc"] = r.get("cc", "")
        st.session_state[k + "_bcc"] = r.get("bcc", "")
        st.session_state[k + "_subj"] = subj
        st.session_state[k + "_topic"] = "General check-in"
        st.session_state[k + "_body"] = (
            f"Hi {r['company']} Team,\n\n"
            "I wanted to follow up on my email below about your Google listing and the audit report I put together. "
            "Did you get a chance to look at it?\n\n"
            "If it would help, I'm happy to go through the findings with you on a short call, whenever suits you.\n\n"
            f"Thanks,\n{cfg.get('sender_name') or '[Your name]'}\n{cfg.get('business') or '[Your business]'}")
        st.session_state[k + "_default"] = st.session_state[k + "_body"]
    st.session_state[k + "_open"] = not st.session_state.get(k + "_open")


def send_followup(fid, r):
    """Runs the moment Send is pressed. The follow-up goes out as a reply: same conversation, 'Re:' subject."""
    k, ss = f"fu_{fid}", st.session_state
    cfg = outreach.load_settings(SETTINGS)
    to_, cc_, bcc_ = ss[k + "_to"].strip(), ss[k + "_cc"].strip(), ss[k + "_bcc"].strip()
    subj_, body_ = ss[k + "_subj"], ss[k + "_body"]
    if ss.get(k + "_quote", True) and r.get("body"):
        body_ += (f"\n\nOn {r['time']}, {cfg.get('sender_name', '')} wrote:\n"
                  + "\n".join("> " + line for line in r["body"].splitlines()))
    try:
        mid = outreach.send_email(to_, subj_, body_, cfg.get("sender_name", ""), cfg.get("gmail", ""),
                                  ss.get("trk_pwd", ""), SMTP_HOST, SMTP_PORT, SMTP_SSL, cc=cc_, bcc=bcc_,
                                  in_reply_to=r.get("message_id") or None, references=r.get("references") or None)
        outreach.log_sent(SENT_LOG, to_, r["company"], subj_)
        outreach.save_sent_copy(SENT_DIR, r.get("search", ""), r["company"], to_, cc_, bcc_, subj_, body_, None,
                                message_id=mid, references=" ".join(x for x in [r.get("references", ""),
                                                                                r.get("message_id", "")] if x),
                                kind="followup")
        ss["trk_flash"] = ("ok", f"Follow-up sent to {to_}.")
        ss[k + "_open"] = False
    except Exception as e:
        msg = str(e)
        if isinstance(e, smtplib.SMTPAuthenticationError) or "535" in msg:
            msg = "Gmail rejected the login. Check the address in settings and use an app password."
        ss["trk_flash"] = ("err", f"Could not send the follow-up: {msg}")


def followup_form(r, fid):
    k = f"fu_{fid}"
    cfg = outreach.load_settings(SETTINGS)
    st.markdown("**Follow-up (sent as a reply to this email)**")
    if not r.get("message_id"):
        st.caption("This email was sent before the tracker saved its ID, so the reply is matched by its 'Re:' subject "
                   "only. Gmail normally groups it into the same conversation, other mail apps may not.")
    st.text_input("To", key=k + "_to")
    c1, c2 = st.columns(2)
    c1.text_input("Cc (optional)", key=k + "_cc")
    c2.text_input("Bcc (optional)", key=k + "_bcc")
    st.text_input("Subject", key=k + "_subj")
    rows, _a = scores_for(r)
    if rows:
        st.selectbox("Follow up about", ["General check-in"] + [f"{l} ({p}%)" for l, p, _ in rows],
                     key=k + "_topic", on_change=pick_topic, args=(fid, r),
                     help="The score areas from this company's audit report. Each one writes a different follow-up. "
                          "Picking another one replaces the text below.")
    else:
        st.caption("The audit report scores are not available for this email, so only the general follow-up is offered.")
    st.text_area("Follow-up text (edit it as you like)", key=k + "_body", height=260)
    st.checkbox("Add the original email below as a quote", value=True, key=k + "_quote")
    problems = []
    if not outreach.EMAIL_RE.match(st.session_state[k + "_to"].strip()):
        problems.append("The 'To' address looks invalid.")
    for label_, key_ in (("Cc", "_cc"), ("Bcc", "_bcc")):
        if outreach.bad_addrs(st.session_state[k + key_]):
            problems.append(f"{label_} has an invalid address.")
    if not (cfg.get("sender_name") and cfg.get("gmail")):
        problems.append("Your name and Gmail address are not saved yet. Save them in step 1 on the Audit and email page.")
    if not st.session_state.get("trk_pwd"):
        problems.append("Enter your Gmail app password at the top of this list.")
    if outreach.sent_today(SENT_LOG) >= int(cfg.get("daily", 30)):
        problems.append("The daily sending limit is reached.")
    for p in problems:
        st.warning(p)
    st.button("Send follow-up", type="primary", key=k + "_send", disabled=bool(problems),
              on_click=send_followup, args=(fid, r))


def show_record(r, n):
    fid = fid_of(r)
    tag = "Follow-up  |  " if r.get("kind") == "followup" else ""
    with st.expander(f"{tag}{r['time']}  |  {r['company']}  |  {r['to']}"):
        st.caption(f"Sent on {r['time']}" + ("  ·  sent before the tracker was added: the text is your saved draft"
                                               if r.get("older") else ""))
        left, right = st.columns([5, 6])
        with left:
            img = SENT_DIR / r["image"] if r.get("image") else None
            cache = st.session_state.setdefault("trk_imgs", {})
            ck = (r["search"], r["company"])
            if img and img.exists():
                st.image(str(img), caption="Audit report image sent with this email")
            elif ck in cache:
                st.image(cache[ck], caption="Report image drawn again from the saved audit")
            elif r.get("older") and r.get("search") and (RESULTS / f"audit_{r['search']}.json").exists():
                st.caption("The original image was not kept for this email.")
                if st.button("Draw the report image again", key=f"redraw_{n}"):
                    with st.spinner("Drawing..."):
                        try:
                            cache[ck] = rebuild_image(r["search"], r["company"])
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not draw it: {e}")
            else:
                st.caption("No report image was kept for this email.")
        with right:
            st.markdown(f"**To:** {r['to']}" + (f"  \n**Cc:** {r['cc']}" if r.get("cc") else "")
                        + (f"  \n**Bcc:** {r['bcc']}" if r.get("bcc") else ""))
            st.markdown(f"**Subject:** {r['subject']}")
            if r.get("body"):
                st.text_area("Email body", r["body"], height=420, disabled=True, key=f"tbody_{n}")
            else:
                st.caption("The text of this email was not saved.")
            st.button("Close the follow-up" if st.session_state.get(f"fu_{fid}_open") else "Write a follow-up",
                      key=f"fubtn_{fid}", on_click=toggle_followup, args=(fid, r))
            if not r.get("older") and r.get("file") and st.button("Delete this saved copy", key=f"delone_{n}",
                                                                    help="Removes the saved text and image only. "
                                                                         "The company stays marked as emailed."):
                (SENT_DIR / r["file"]).unlink(missing_ok=True)
                if r.get("image"):
                    (SENT_DIR / r["image"]).unlink(missing_ok=True)
                st.rerun()
        if st.session_state.get(f"fu_{fid}_open"):
            st.divider()
            followup_form(r, fid)


saved = outreach.read_sent_copies(SENT_DIR)
everything = saved + older_records(saved)
stems = {s["file"].stem for s in in_country if s["city"] == city}
sent = sorted([r for r in everything if r.get("search") in stems], key=lambda r: r["time"], reverse=True)
st.subheader(f"{category.title()} in {city}, {country}")
if not sent:
    st.info("No sent emails found for this search.")
else:
    st.write(f"**{len(sent)}** email(s) sent. Newest first.")
    st.text_input("Gmail app password (only needed to send follow-ups, it is never saved)", type="password",
                  key="trk_pwd")
    for n, r in enumerate(sent):
        show_record(r, f"c{n}")

unknown = sorted([r for r in everything if not r.get("search")], key=lambda r: r["time"], reverse=True)
if unknown:
    with st.expander(f"Sent earlier, search not known ({len(unknown)})"):
        st.caption("These are in your sent log but no saved draft was found, so they cannot be placed under a city.")
        st.dataframe([{"Sent": r["time"], "Company": r["company"], "To": r["to"], "Subject": r["subject"]}
                      for r in unknown], hide_index=True)

# ------------------------------------------------------------------ delete history
st.divider()
with st.expander("Delete history"):
    st.caption("Pick one search and choose what to remove. Nothing is deleted until you tick the box and press the button.")
    pick = st.selectbox("Search", SEARCHES, format_func=lambda s: f"{s['query']}  ({s['file'].name})", key="del_pick")
    stem = pick["file"].stem
    c_audit = st.checkbox("Its audit report, drafts, extra details and typed emails", key="del_audit")
    c_copies = st.checkbox("The saved copies of emails sent from it (text and report images)", key="del_copies")
    c_log = st.checkbox("Its entries in the sent log (the companies can be emailed again)", key="del_log",
                        help="Without these entries the app no longer knows these companies were emailed, so it will "
                             "let you email them again.")
    c_file = st.checkbox("The search results file itself (the list of companies)", key="del_file",
                         help="It also disappears from the dropdowns above.")
    sure = st.checkbox("Yes, delete the ticked items. This cannot be undone.", key="del_sure")
    if st.button("Delete", type="primary", disabled=not (sure and (c_audit or c_copies or c_log or c_file)),
                 key="del_go"):
        done = []
        # worked out first, because deleting the audit also deletes the drafts that tell us who was emailed
        copies = [r for r in saved if r.get("search") == stem]
        gone = {r["to"].lower() for r in copies}
        try:
            drafts = json.loads((RESULTS / f"drafts_{stem}.json").read_text(encoding="utf-8"))
        except Exception:
            drafts = {}
        gone |= {(d.get("to") or "").strip().lower() for d in drafts.values()}
        if c_audit:
            for pre in ("audit_", "drafts_", "extra_", "emails_"):
                (RESULTS / f"{pre}{stem}.json").unlink(missing_ok=True)
            for k_ in [k_ for k_ in st.session_state if str(k_).startswith(("audits", "drafts", "img_cache"))]:
                st.session_state.pop(k_, None)
            done.append("audit report and drafts")
        if c_log:
            # the log has no search name, so remove the rows of the companies this search emailed
            import csv
            rows = [r for r in outreach.read_log(SENT_LOG) if r["email"].lower() not in gone]
            with open(SENT_LOG, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=outreach.LOG_FIELDS)
                w.writeheader()
                w.writerows(rows)
            done.append("sent log entries")
        if c_copies:
            for f in SENT_DIR.glob("*.json"):
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if rec.get("search") == stem:
                    if rec.get("image"):
                        (SENT_DIR / rec["image"]).unlink(missing_ok=True)
                    f.unlink(missing_ok=True)
            done.append("saved email copies")
        if c_file:
            for suffix in (".xlsx", ".meta.json"):
                (RESULTS / f"{stem}{suffix}").unlink(missing_ok=True)
            done.append("search results file")
        st.session_state["trk_deleted"] = "Deleted: " + ", ".join(done) + f" for {pick['query']}."
        st.rerun()
msg = st.session_state.pop("trk_deleted", None)
if msg:
    st.success(msg)