"""
report.py - the audit report, built with reportlab on the Starkology pad.

Two outputs from the same data:
  * build_image(...)  one tall JPG (all four short sections on the pad). It is attached to the email body.
  * build_pdf(...)    the full report (12 sections) as a multi-page PDF on the same pad, for download.

Rules, copied from the sample report:
  * nothing is invented. A detail the scraper could not read is left out of the scores and listed in a note;
  * scores are rule-based (see compute_scores), not platform-generated metrics.
"""

import re
import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from xml.sax.saxutils import escape

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Flowable, Frame, HRFlowable, KeepTogether,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

from audit import CHECKS, _to_float
from outreach import city_from, clean_query

NAVY = colors.HexColor("#13294B")
TEAL = colors.HexColor("#0E7C86")
ORANGE = colors.HexColor("#C46A00")
RED = colors.HexColor("#C0392B")
GREEN = colors.HexColor("#1E8449")
GREY = colors.HexColor("#6B7280")
LIGHT = colors.HexColor("#F3F6F9")
CALL_BG = colors.HexColor("#E8F4F4")
LINE = colors.HexColor("#D5DCE4")
INK = colors.HexColor("#1F2937")

# ------------------------------------------------------------------- the pad
# assets/pad.png is the Starkology teal pad (1500 x 2800 px). Only its top and bottom artwork is used; the
# plain white middle is simply extended, so the pad grows to any height without stretching the logo.
ASSETS = Path(__file__).parent / "assets"
PAD_FILE = ASSETS / "pad.png"
PAD_W = 1500
PAD_TOP_ROWS = (0, 480)        # header artwork
PAD_BOTTOM_ROWS = (2260, 2800)  # footer artwork
PAD_RULE_ROW = 258             # the green line under the logo
_slices = {}


def _pad_slices():
    """(top image, bottom image) as ImageReaders, or None if the pad file is missing."""
    if "v" not in _slices:
        if not PAD_FILE.exists():
            _slices["v"] = None
        else:
            im = Image.open(PAD_FILE).convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[3])
            out = []
            for a, b in (PAD_TOP_ROWS, PAD_BOTTOM_ROWS):
                buf = BytesIO()
                bg.crop((0, a, PAD_W, b)).save(buf, "PNG")
                buf.seek(0)
                out.append(ImageReader(buf))
            _slices["v"] = tuple(out)
    return _slices["v"]


def draw_pad(c, page_w, page_h):
    """Paint the pad on a canvas: header art at the top, footer art at the bottom, white between."""
    sl = _pad_slices()
    if not sl:
        return
    k = page_w / PAD_W
    th = (PAD_TOP_ROWS[1] - PAD_TOP_ROWS[0]) * k
    bh = (PAD_BOTTOM_ROWS[1] - PAD_BOTTOM_ROWS[0]) * k
    c.drawImage(sl[0], 0, page_h - th, width=page_w, height=th)
    c.drawImage(sl[1], 0, 0, width=page_w, height=bh)


# -------------------------------------------------------------------- layout
# One report is laid out at a time. The page width decides every column width, so the same code makes the
# wide A4 PDF and the narrower, bigger-text email image.
_LOCK = threading.RLock()
SIDE = 0.0457          # the pad's rule line starts 4.6 % in from each side
PW = A4[0]
W = PW * (1 - 2 * SIDE)
KEEP = 35 * mm          # room a heading needs on the page (0 on the one-page image)


def _layout(pw, keep=35 * mm):
    global PW, W, KEEP
    PW = pw
    W = pw * (1 - 2 * SIDE)
    KEEP = keep


def S(mm_value):
    """A column width given in mm for a 174 mm wide table, scaled to the current width."""
    return mm_value * mm * W / (174 * mm)


def _hex(c):
    return "#" + c.hexval()[2:]


def _style(name, **kw):
    base = dict(fontName="Helvetica", fontSize=8.8, leading=12, textColor=INK)
    base.update(kw)
    return ParagraphStyle(name, **base)


BODY = _style("body")
CELL = _style("cell", fontSize=8.2, leading=10.6)
CELLB = _style("cellb", fontSize=8.2, leading=10.6, fontName="Helvetica-Bold")
HDR = _style("hdr", fontSize=8, leading=10, fontName="Helvetica-Bold", textColor=colors.white)
NOTE = _style("note", fontSize=7.4, leading=9.6, fontName="Helvetica-Oblique", textColor=GREY)
H1 = _style("h1", fontSize=14, leading=17, fontName="Helvetica-Bold", textColor=NAVY, spaceBefore=10, spaceAfter=3)
H2 = _style("h2", fontSize=10.5, leading=14, fontName="Helvetica-Bold", textColor=NAVY, spaceBefore=8,
            spaceAfter=3, keepWithNext=1)
CHIP = _style("chip", fontSize=7.4, leading=9.2, fontName="Helvetica-Bold", textColor=colors.white, alignment=1)
SCORE = _style("score", fontSize=12, leading=14, fontName="Helvetica-Bold", alignment=1)

STATUS_COLOR = {
    "GOOD": TEAL, "OK": TEAL, "STRONG": GREEN, "ACTIVE": TEAL, "YES": TEAL,
    "NEEDS WORK": ORANGE, "PARTIAL": ORANGE, "IMPORTANT": ORANGE, "LOW": ORANGE, "INACTIVE": RED,
    "CRITICAL": RED, "MISSING": RED, "INCOMPLETE": RED, "NO": RED,
    "ENHANCE": GREY, "UNVERIFIED": GREY, "NOT MEASURED": GREY, "INFO": GREY, "NOT USED": GREY,
}


def tx(s):
    """Text made safe for a Paragraph: Latin-1/cp1252 only, XML escaped."""
    s = "" if s is None else str(s)
    return escape(s.encode("cp1252", "replace").decode("cp1252"))


def P(s, st=CELL):
    return Paragraph(tx(s), st)


def stat(label, dagger=False):
    up = label.upper()
    key = next((k for k in sorted(STATUS_COLOR, key=len, reverse=True) if up.startswith(k)), None)
    color = STATUS_COLOR.get(key, INK)
    return Paragraph(f'<font color="{_hex(color)}"><b>{tx(label)}</b></font>' + (" †" if dagger else ""), CELL)


def band(score):
    if score is None:
        return "NOT MEASURED", GREY
    if score < 25:
        return "CRITICAL", RED
    if score < 50:
        return "NEEDS WORK", ORANGE
    if score <= 75:
        return "GOOD", TEAL
    return "STRONG", GREEN


# --------------------------------------------------------------------- tables
def table(header, rows, widths, zebra=True, extra_style=None):
    data = [[Paragraph(tx(h), HDR) for h in header]] + rows
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("BOX", (0, 0), (-1, -1), 0.4, LINE),
    ]
    if zebra:
        st += [("BACKGROUND", (0, i), (-1, i), LIGHT) for i in range(2, len(data), 2)]
    t.setStyle(TableStyle(st + (extra_style or [])))
    return t


def kv_table(rows):
    data = [[P(k, CELLB), v if not isinstance(v, str) else P(v)] for k, v in rows]
    t = Table(data, colWidths=[S(42), W - S(42)])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), LIGHT), ("BACKGROUND", (1, 0), (1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("BOX", (0, 0), (-1, -1), 0.4, LINE),
    ]))
    return t


def callout(title, paras, color=TEAL):
    inner = [Paragraph(f'<font color="{_hex(color)}"><b>{tx(title)}</b></font>', BODY)]
    inner += [Paragraph(p, BODY) if p.startswith("<") else P(p, BODY) for p in paras]
    t = Table([[inner]], colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CALL_BG), ("LINEBEFORE", (0, 0), (0, 0), 3, color),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return KeepTogether([Spacer(1, 4), t, Spacer(1, 4)])


def heading(n, title):
    """Section heading. It needs KEEP points of room on the page, so it is never stranded at the bottom."""
    return [CondPageBreak(KEEP), Paragraph(f'<font color="{_hex(TEAL)}">{n}</font>&nbsp;&nbsp;{tx(title)}', H1),
            HRFlowable(width="100%", thickness=0.8, color=TEAL, spaceAfter=6)]


# -------------------------------------------------------------------- scoring
def _wavg(items):
    known = [(w, v) for w, v in items if v is not None]
    return sum(w * v for w, v in known) / sum(w for w, _ in known) if known else None


def _yn(v):
    return {"Yes": 100, "No": 0}.get(v)


def _tf(v):
    return None if v is None else (100 if v else 0)


def review_target(comps):
    """(target review count, whether it comes from the competitors found in the same search)."""
    counts = [c["reviews"] for c in comps if c.get("reviews") is not None]
    if len(counts) >= 3:
        return round(min(150, max(25, median(counts)))), True
    return 50, False


def compute_scores(a, extra, comps):
    """Six category scores (0-100 or None when not measured) plus the overall score."""
    cb = {c["id"]: c["ok"] for c in a["checks"]}
    site_ok = a["page_status"] == "ok"
    reviews = a["facts"].get("reviews")
    rating = _to_float(a["facts"].get("rating"))
    cats = []

    # GBP optimisation: needs at least two details from the live profile / GMB Everywhere
    sc, ph, sv = extra.get("secondary_categories"), extra.get("photos"), extra.get("services")
    contact = 100 if (a.get("phone") and a.get("website")) else 50 if (a.get("phone") or a.get("website")) else 0
    items = [
        (15, _yn(extra.get("claimed"))), (10, contact),
        (20, None if sc is None else (0 if sc == 0 else 60 if sc == 1 else 100)),
        (20, None if ph is None else (20 if ph < 10 else 60 if ph < 25 else 100)),
        (15, None if sv is None else (0 if sv == 0 else 50 if sv < 6 else 100)),
        (15, _yn(extra.get("description"))), (5, _yn(extra.get("hours"))),
    ]
    known = sum(1 for i, (_, v) in enumerate(items) if i != 1 and v is not None)
    gbp = _wavg(items) if known >= 2 else None
    missing = [n for n, v in (("categories", sc), ("photos", ph), ("services", sv)) if v is None]
    if gbp is None:
        basis = "Not measured: the Maps page showed too little of the profile. Enter the details from GMB Everywhere."
    else:
        bits = []
        if extra.get("claimed"):
            bits.append("claimed" if extra["claimed"] == "Yes" else "not claimed")
        if extra.get("hours"):
            bits.append("hours " + ("listed" if extra["hours"] == "Yes" else "missing"))
        if sc is not None:
            bits.append(f"{sc} secondary categor{'y' if sc == 1 else 'ies'}")
        if ph is not None:
            bits.append(f"{ph} photos")
        if sv is not None:
            bits.append(f"{sv} services")
        if extra.get("description"):
            bits.append("description " + ("set" if extra["description"] == "Yes" else "missing"))
        basis = "Based on: " + ", ".join(bits) + "."
        if missing:
            gbp = min(gbp, 65)  # never look STRONG while the big items are unknown
            basis += " Provisional: " + ", ".join(missing) + " not captured \u2020."
    cats.append(("GBP Optimization", gbp, basis))

    # Keyword targeting: how well the homepage speaks the local search language
    kitems = [(4, _tf(cb.get("title_local"))), (2, _tf(cb.get("h1"))), (2, _tf(cb.get("meta_desc"))),
              (2, _tf(cb.get("schema")))]
    kn = sum(1 for _, v in kitems if v is not None)
    kw = _wavg(kitems) if site_ok and kn >= 2 else None
    if kw is None:
        basis = "Not measured: the homepage could not be checked, or no city was entered."
    else:
        bad = [CHECKS[i][0].lower() for i in ("title_local", "h1", "meta_desc", "schema") if cb.get(i) is False]
        basis = ("Homepage title, headings, description and structured data are in order." if not bad else
                 "Homepage issues: " + ", ".join(bad) + ". No ranking baseline yet.")
    cats.append(("Keyword Targeting", kw, basis))

    # Citations: directory counts if entered by hand, else what the homepage itself shows
    d = extra.get("directories")
    profiles = a["facts"].get("profiles") or []
    if d is not None:
        cit = min(100, d / 8 * 100)
        cbasis = f"{d} of 8 core directories confirmed with the same name, address and phone."
    elif site_ok and (cb.get("nap") is not None or profiles or a["facts"].get("title")):
        cit = _wavg([(2, _tf(cb.get("nap"))), (2, 0 if not profiles else 50 if len(profiles) == 1 else 100)])
        cbasis = ("Homepage only: phone " + {True: "matches", False: "does not match", None: "not compared with"}[cb.get("nap")]
                  + " the Google listing; " + (f"links to {', '.join(profiles)}" if profiles else "no business profile links")
                  + ". Directory listings (Apple, Bing, Yelp) not checked \u2020.")
    else:
        cit, cbasis = None, "Not measured: the homepage could not be checked and no directories were entered."
    cats.append(("Citation Strength", cit, cbasis))

    # Reviews: rating quality plus volume against the competitors found in the same search
    if reviews is None and rating is None:
        rev, basis = None, "Not measured: no review data."
    else:
        target, typical = review_target(comps)
        vol = None if reviews is None else min(1, reviews / target) * 100
        rate = None if rating is None else max(0, min(1, (rating - 4.0) / 0.8)) * 100
        rev = _wavg([(2, vol), (1, rate)])  # volume counts double: it is the usual gap
        basis = (f"{reviews if reviews is not None else '?'} reviews at {a['facts'].get('rating') or '?'} stars; volume is "
                 f"compared with a target of about {target} reviews"
                 + (" (the typical competitor in this search)." if typical else "."))
    cats.append(("Review Management", rev, basis))

    # Posts: needs the date of the last post
    m = extra.get("last_post_months")
    post = None if m is None else (5 if m >= 99 else 85 if m <= 1 else 55 if m == 2 else 30 if m == 3 else 20 if m <= 6 else 10)
    cats.append(("GMB Post Activity", post,
                 "Not measured: the date of the last post was not captured." if m is None else
                 "No posts ever." if m >= 99 else f"Last post about {m} month{'' if m == 1 else 's'} ago."))

    # Website: share of website-check severity that passed
    wk = [c for c in a["checks"] if c["area"] == "Website" and c["ok"] is not None]
    if site_ok and wk:
        tot = sum(c["severity"] for c in wk)
        bad = sum(c["severity"] for c in wk if c["ok"] is False)
        web = 100 * (1 - bad / tot)
        fails = [c["label"].lower() for c in sorted(wk, key=lambda c: -c["severity"]) if c["ok"] is False][:3]
        basis = "Homepage check: " + (", ".join(fails) + " need attention." if fails else "no problems found.")
    else:
        web, basis = None, "Not measured: " + (a["page_status"] or "no website.")
    cats.append(("Website / SEO", web, basis))

    measured = [s for _, s, _ in cats if s is not None]
    overall = round(sum(measured) / len(measured)) if len(measured) >= 2 else None
    return {"cats": [(n, None if s is None else round(s), b) for n, s, b in cats], "overall": overall,
            "measured": len(measured)}


# -------------------------------------------------------------------- actions
SEV_ORDER = {"CRITICAL": 0, "IMPORTANT": 1, "ENHANCE": 2}


def build_actions(a, extra, comps):
    cb = {c["id"]: c["ok"] for c in a["checks"]}
    acts = []

    def add(sev, title, desc, timeline, effort, phase, todo):
        acts.append({"sev": sev, "title": title, "desc": desc, "timeline": timeline, "effort": effort,
                     "phase": phase, "todo": todo})

    m = extra.get("last_post_months")
    if m is not None and m >= 2:
        add("CRITICAL", "Restart GBP posting cadence",
            "One post per week using real jobs, a photo and a call button. Prepare 8 posts in advance from recent work.",
            "Week 1", "Low", 1, "Publish the first 2 GBP posts and schedule weekly posts")
    elif m is None:
        add("ENHANCE", "Check posting cadence and set a weekly routine",
            "The date of the last post was not captured. If the profile has been quiet, post weekly with a photo and a button.",
            "Week 1-2", "Low", 1, "Check the date of the last GBP post and start a weekly post routine")
    reviews_now = a["facts"].get("reviews")
    if cb.get("reviews_low") is False or (reviews_now is not None and reviews_now < review_target(comps)[0]):
        add("CRITICAL", "Launch a review-generation system",
            "Send the Google review link by text right after every job and reply to every review within 48 hours. "
            "Ask customers to mention the service and area.", "Week 1-2", "Low", 1,
            "Start post-job review requests with the Google review link; reply to all existing reviews")
    if cb.get("rating_low") is False:
        add("CRITICAL", "Recover the rating",
            "Reply professionally to critical reviews, fix the complaints that keep coming back, and ask recent happy "
            "customers for reviews.", "Week 1-4", "Medium", 1, "Reply to every critical review and log the recurring complaints")
    sc, ph, sv, de = (extra.get(k) for k in ("secondary_categories", "photos", "services", "description"))
    if sc is not None and sc < 2:
        add("CRITICAL", "Add secondary categories and attributes",
            "Add relevant secondary categories that match real services, and complete every applicable attribute.",
            "Week 1", "Low", 1, "Add secondary categories and complete all attributes")
    if (sv is not None and sv < 6) or de == "No":
        add("IMPORTANT", "Rebuild the services list and description",
            "Add every service you offer with a short description, and use the full 750-character business description "
            "with the areas you serve.", "Week 2", "Medium", 2, "Rewrite the business description and expand the services list")
    if ph is not None and ph < 25:
        add("IMPORTANT", "Upload a photo set",
            "Add 25+ original photos: team, vehicle, premises and before/after work.", "Week 2-3", "Low", 2,
            "Upload 25+ original photos")
    if None in (sc, ph, sv) or not de:
        add("ENHANCE", "Audit the live profile for completeness",
            "Categories, services, description, photos or attributes were not captured. Check each on the live profile "
            "(GMB Everywhere helps) and fill the gaps. Also rule out duplicate listings.", "Week 1", "Low", 1,
            "Check the profile for missing details and duplicate listings")
    add("IMPORTANT", "Capture a geo-grid baseline",
        "No ranking data was available. Run a map-pack scan across the areas you serve for 3-5 core keywords so every "
        "later result can be measured.", "Week 1", "Low", 1, "Capture a geo-grid baseline for the core keywords")

    if cb.get("ssl_invalid") is False:
        add("CRITICAL", "Fix the website security certificate",
            "Renew or reinstall the certificate so visitors stop seeing a browser warning, and redirect every page to HTTPS.",
            "Week 1", "Low", 1, "Fix the website certificate and force HTTPS")
    elif cb.get("https") is False:
        add("CRITICAL", "Move the website to HTTPS",
            "Install a certificate and redirect every page to the secure version.", "Week 1", "Low", 1,
            "Move the website to HTTPS")
    if cb.get("mobile") is False:
        add("CRITICAL", "Make the website mobile-friendly",
            "Use a responsive layout so the site reads and taps properly on a phone, with the phone number one tap away.",
            "Week 1-3", "Medium", 2, "Make the website responsive on phones")
    seo_text = {"title_local": "add the city to the title and main heading", "h1": "add a main heading (H1)",
                "meta_desc": "write a meta description"}
    seo = [seo_text[i] for i in seo_text if cb.get(i) is False]
    if seo:
        add("IMPORTANT", "Rewrite title, headings and description for local search",
            "To do: " + ", ".join(seo) + ". Build them around what you do and where.", "Week 2", "Medium", 2,
            "Rewrite the homepage title tag, H1 and meta description")
    if cb.get("title_local") is False:
        add("IMPORTANT", "Create service and area landing pages",
            "Build one page per main service and area you serve, with unique titles and your name, address and phone.",
            "Month 2", "High", 3, "Publish service + area landing pages")
    if cb.get("schema") is False:
        add("IMPORTANT", "Add LocalBusiness schema",
            "Add structured data that matches the Google Business Profile exactly, and test it in the Rich Results tool.",
            "Month 2", "Medium", 3, "Add LocalBusiness schema and test it in the Rich Results tool")
    tidy_text = {"tel_link": "add a tap-to-call link", "sitemap": "add an XML sitemap", "stale_year": "update the footer year",
                 "alt_text": "add image alt text", "maps_embed": "add a Google Map or a link to the listing",
                 "heavy": "slim down the homepage"}
    tidy = [tidy_text[i] for i in tidy_text if cb.get(i) is False]
    if tidy:
        add("ENHANCE", "Website tidy-up", "To do: " + ", ".join(tidy) + ".", "Month 2", "Low", 3,
            "Website tidy-up: " + ", ".join(tidy))
    if a["page_status"] != "ok":
        add("ENHANCE", "Check the website by hand",
            "The website could not be checked automatically (" + (a["page_status"] or "no website") + ").",
            "Week 1", "Low", 1, "Check the website manually")

    d = extra.get("directories")
    add("IMPORTANT" if d is None or d < 6 else "ENHANCE", "Lock the NAP and build core citations",
        "Pick one exact name, address and phone format, then list it identically on Apple Business Connect, Bing Places, "
        "Facebook, Yelp and the main local directories. Use the clean website domain.", "Week 3-8", "Medium", 2,
        "Fix the NAP master record and claim or correct Apple, Bing and Facebook listings")
    add("ENHANCE", "Earn local links and mentions",
        "Pursue local business directories, sponsorships and a short local-press feature.", "Month 3", "Medium", 4,
        "Pursue 3-5 local links and one local-press feature")
    acts.sort(key=lambda x: SEV_ORDER[x["sev"]])
    return acts


# -------------------------------------------------------------------- helpers
def _area(a, settings):
    terms = [t.strip() for t in (settings.get("city_terms") or "").split(",") if t.strip()]
    if terms:
        return terms[0]
    parts = [p.strip() for p in (a.get("address") or "").split(",") if p.strip()]
    return ", ".join(parts[-2:]) if parts else "Local area"


def _months(n):
    return f"{n} month{'' if n == 1 else 's'}"


def _num(v):
    return f"{v:,}" if isinstance(v, int) else ("-" if v in (None, "") else str(v))


def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "company"




# ======================================================================= shared data
def merged_extra(a, manual):
    """Details read from the Maps page, overridden by anything typed in by hand."""
    out = dict(a.get("gbp") or {})
    out.update({k: v for k, v in (manual or {}).items() if v not in (None, "")})
    return out


def _top_of_search(comps, n=3):
    return sorted((c for c in comps if c.get("rank")), key=lambda c: c["rank"])[:n]


def _prepare(a, extra, others, settings, query, rank, total):
    """Everything both reports need, worked out once."""
    r = SimpleNamespace(a=a, rank=rank, total=total, settings=settings)
    r.extra = merged_extra(a, extra)
    r.comps = [c for c in others if c.get("name") and c["name"].strip().lower() != a["company"].strip().lower()]
    r.sc = compute_scores(a, r.extra, r.comps)
    r.acts = build_actions(a, r.extra, r.comps)
    r.cb = {c["id"]: c["ok"] for c in a["checks"]}
    r.facts = a["facts"]
    r.reviews, r.rating = r.facts.get("reviews"), _to_float(r.facts.get("rating"))
    r.today = datetime.now().strftime("%d %B %Y").lstrip("0")
    r.q = clean_query(query)
    r.area = city_from(r.q, a.get("address", "")) or "Local area"
    r.biz = (settings.get("business") or "").strip()
    r.cat = (a.get("category") or "").strip()
    r.tgt, r.typical = review_target(r.comps)
    r.lp = r.extra.get("last_post_months")
    r.top = _top_of_search(r.comps)
    r.best = sorted((c for c in r.comps if c.get("reviews")), key=lambda c: -c["reviews"])[:3]
    ex = r.extra
    strengths, weak = [], []
    if r.rating and r.rating >= 4.5:
        strengths.append(f"a {r.facts.get('rating')} Google rating")
    if r.reviews and r.reviews >= r.tgt:
        strengths.append(f"{r.reviews} Google reviews")
    if r.cb.get("https") and r.cb.get("mobile"):
        strengths.append("a secure, mobile-friendly website")
    if ex.get("claimed") == "Yes":
        strengths.append("a claimed profile")
    if ex.get("hours") == "Yes":
        strengths.append("opening hours listed")
    low_reviews = r.cb.get("reviews_low") is False or (r.reviews is not None and r.reviews < r.tgt)
    for label, bad in [(f"low review volume ({r.reviews})" if r.reviews is not None else "low review volume", low_reviews),
                       (f"a {r.facts.get('rating')} rating", r.cb.get("rating_low") is False),
                       ("no HTTPS", r.cb.get("https") is False),
                       ("an invalid security certificate", r.cb.get("ssl_invalid") is False),
                       ("no mobile setup", r.cb.get("mobile") is False),
                       (f"{r.area} missing from the homepage title and headings", r.cb.get("title_local") is False),
                       ("no local business schema", r.cb.get("schema") is False),
                       ("an unclaimed profile", ex.get("claimed") == "No"),
                       ("no opening hours", ex.get("hours") == "No"),
                       ("no business description", ex.get("description") == "No"),
                       ("no posts" if r.lp == 99 else f"a quiet profile (last post about {_months(r.lp or 0)} ago)",
                        r.lp is not None and r.lp >= 2)]:
        if bad:
            weak.append(label)
    if rank and total and rank > 3:
        weak.insert(0, f"position #{rank} of {total} in the Maps search")
    r.strengths, r.weak = strengths, weak
    # what the Maps page did not show, so it is not in the scores
    r.not_captured = [n for n, v in (("secondary categories", ex.get("secondary_categories")), ("photos", ex.get("photos")),
                                     ("services", ex.get("services")), ("business description", ex.get("description")),
                                     ("date of the last post", ex.get("last_post_months"))) if v in (None, "")]
    return r


def _cover(r):
    st = lambda n, **kw: _style(n, **kw)
    cover = Table([[[
        Paragraph('<font color="#5FD0D8"><b>LOCAL SEO AUDIT REPORT</b></font>', st("c1", fontSize=7.5, textColor=colors.white)),
        Spacer(1, 6),
        Paragraph(tx(r.a["company"]), st("c2", fontSize=19, leading=23, fontName="Helvetica-Bold", textColor=colors.white)),
        Spacer(1, 5),
        Paragraph(tx(f"Google Business Profile & Local Search Visibility  |  {r.area}"),
                  st("c3", fontSize=9, leading=12, textColor=colors.HexColor("#C9D6EA"))),
        Spacer(1, 6),
        Paragraph(tx(f"Prepared {r.today}"), st("c4", fontSize=7.5, textColor=colors.HexColor("#9FB3D1"))),
    ]]], colWidths=[W])
    cover.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 16),
                               ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14)]))
    return [cover, Spacer(1, 4)]


def _status_cell(label):
    up = label.upper()
    key = next((k for k in sorted(STATUS_COLOR, key=len, reverse=True) if up.startswith(k)), None)
    return Paragraph(tx(label), CHIP), STATUS_COLOR.get(key, GREY)


# ======================================================================= sections 1-4
def short_story(r):
    """Cover + the four sections of the short report."""
    a, ex, facts = r.a, r.extra, r.facts
    sc_, ph_, sv_, de_ = (ex.get(k) for k in ("secondary_categories", "photos", "services", "description"))
    ob, oc = band(r.sc["overall"])
    story = _cover(r)

    # ---- 1 profile overview
    story += heading(1, "Business Profile Overview")
    status_bits = []
    if ex.get("claimed"):
        status_bits.append("Claimed" if ex["claimed"] == "Yes" else "NOT claimed")
    if ex.get("hours"):
        status_bits.append("Opening hours listed" if ex["hours"] == "Yes" else "Opening hours missing")
    cat_txt = r.cat or "Not captured"
    if sc_ is not None:
        cat_txt += f" (+{sc_} secondary)" if sc_ else " (no secondary categories set)"
    summary = ("Held back by: " + ", ".join(r.weak[:3])) if r.weak else "No serious gaps confirmed"
    rows = [("Business name", a["company"]), ("Address", a.get("address") or "Not captured"),
            ("Phone", a.get("phone") or "Not listed"), ("Website", a.get("website") or "Not listed"),
            ("Categories", cat_txt),
            ("GBP reviews / rating", f"{_num(r.reviews)} reviews  |  {facts.get('rating') or '-'} average rating")]
    if status_bits:
        rows.append(("Profile status", " | ".join(status_bits)))
    if sv_ is not None:
        rows.append(("Services listed on GBP", f"{sv_} services"))
    if r.lp is not None:
        rows.append(("Last GBP post", "Never" if r.lp >= 99 else "This month" if r.lp == 0 else f"Approx. {_months(r.lp)} ago"))
    if r.rank:
        rows.append(("Position in this search",
                     f"#{r.rank} of {r.total} in the Google Maps list for \"{r.q}\" (indicative: it depends on where the "
                     "search is made from)"))
    rows.append(("Overall status", Paragraph(
        f'<font color="{_hex(oc)}"><b>{"n/a" if r.sc["overall"] is None else str(r.sc["overall"]) + "%"}</b></font>&nbsp;'
        f'<font color="{_hex(oc)}"><b>{ob}</b></font>&nbsp;&nbsp;<font color="#6B7280">{tx(summary)}</font>', CELL)))
    story += [kv_table(rows)]
    story += [Spacer(1, 3), Paragraph(
        "Findings come from the business's Google Maps listing, a check of its homepage and the other businesses in the "
        "same Maps search. Nothing has been estimated or invented.", NOTE)]

    # ---- 2 scorecard
    story += heading(2, "Overall Audit Scorecard")
    story += [P("Each category is scored 0-100% on the evidence available. Bands: CRITICAL below 25%, NEEDS WORK 25-50%, "
                "GOOD 50-75%, STRONG above 75%. A category with no data is shown as NOT MEASURED and is left out of the "
                "overall score.", BODY), Spacer(1, 5)]
    cw = [S(33), S(15), S(26), W - S(74)]
    rows, style = [], []
    allrows = r.sc["cats"] + [("OVERALL", r.sc["overall"], f"Average of the {r.sc['measured']} measured categories above.")]
    for i, (name, score, basis) in enumerate(allrows, 1):
        b, col = band(score)
        last = name == "OVERALL"
        rows.append([
            Paragraph(f"<b>{name}</b>", _style("n", fontSize=8.4, textColor=colors.white if last else NAVY)),
            Paragraph("n/a" if score is None else f"{score}%", _style("s", parent=SCORE, textColor=colors.white if last else col,
                                                                      fontSize=11, fontName="Helvetica-Bold", alignment=1)),
            Paragraph(b, CHIP),
            P(basis, _style("bs", fontSize=7.6, leading=9.8, textColor=colors.white if last else INK)),
        ])
        style += [("BACKGROUND", (2, i), (2, i), col), ("VALIGN", (0, i), (-1, i), "MIDDLE")]
        if last:
            style += [("BACKGROUND", (0, i), (1, i), NAVY), ("BACKGROUND", (3, i), (3, i), NAVY)]
    story += [table(["Category", "Score", "Status", "Basis"], rows, cw, extra_style=style)]
    story += [Spacer(1, 3), Paragraph("Scores are rule-based assessments of the data collected, not platform-generated metrics.", NOTE)]
    story += [callout("Where the score is won and lost", [
        "Strengths: " + (", ".join(r.strengths) if r.strengths else "a live Google listing and website") + ".",
        "Weaknesses: " + (", ".join(r.weak[:5]) if r.weak else "none confirmed from the data captured") + "."])]

    # ---- 3 GBP analysis
    story += heading(3, "Google Business Profile Analysis")
    top, cat, tgt = r.top, r.cat, r.tgt
    DASH = "-"

    def comp_cats():
        cs = [(c["name"], c.get("category")) for c in top if c.get("category")]
        if not cs:
            return DASH
        same = cat and all((x or "").lower() == cat.lower() for _, x in cs)
        return ("The top results use the same primary category (" + cat + ")." if same else
                "Top results are listed as: " + "; ".join(f"{n} ({c})" for n, c in cs) + ".")

    comp_rev = ("Most reviewed in this search: " + "; ".join(
        f"{c['name']} {c['reviews']:,} reviews" + (f" at {c['rating']}" if c.get("rating") else "") for c in r.best) + "."
                if r.best else DASH)
    known_d = [c for c in top if c.get("description") in ("Yes", "No")]
    comp_desc = (f"{sum(1 for c in known_d if c['description'] == 'Yes')} of the top {len(known_d)} results have a business "
                 "description set." if known_d else DASH)
    comp_site = f"{sum(1 for c in top if c.get('website'))} of the top {len(top)} results list a website." if top else DASH

    if r.rating is not None and r.rating < 4.3:
        gs = "NEEDS WORK"
    elif r.reviews is not None and r.reviews < tgt:
        gs = "LOW volume"
    else:
        gs = "GOOD"
    nap = next((c for c in a["checks"] if c["id"] == "nap"), None)
    prof = facts.get("profiles") or []
    site_ok = a["page_status"] == "ok"
    web_txt = (((nap["detail"] + ". ") if nap and nap["ok"] is not None else "") +
               (f"Homepage links to {', '.join(prof)}." if prof else "Homepage links to no social or review profiles.")
               if site_ok else f"The website could not be checked ({a['page_status'] or 'no website'}).")
    gaps = []
    if de_ is not None:
        gaps.append(("Business description", "OK" if de_ == "Yes" else "MISSING",
                     "A description is set on the Maps About tab. Check that it uses the full 750 characters and names the areas served."
                     if de_ == "Yes" else
                     "No description was found on the Maps About tab. Add one covering core services, areas served and hours, "
                     "without repeating the business name.", comp_desc))
    gaps.append(("Primary / secondary categories",
                 "PARTIAL" if sc_ is None else "INCOMPLETE" if sc_ == 0 else "PARTIAL" if sc_ == 1 else "OK",
                 f"Primary category: {cat or 'not captured'}. "
                 + ("" if sc_ is None else f"{sc_} secondary categories set. ")
                 + "Add only categories that match real services, so the profile can appear for related searches.", comp_cats()))
    if ph_ is not None:
        gaps.append(("Photos", "INCOMPLETE" if ph_ < 10 else "PARTIAL" if ph_ < 25 else "OK",
                     f"{ph_} photos on the profile. Keep a fresh library of the team, vehicle, premises and finished work. Aim for 25+.", DASH))
    if sv_ is not None:
        gaps.append(("Services listed", "MISSING" if sv_ == 0 else "PARTIAL" if sv_ < 6 else "OK",
                     f"{sv_} services listed. List every service you offer with a short description, filed under the right category.", DASH))
    gaps.append(("Reviews & rating", gs,
                 f"{_num(r.reviews)} reviews at {facts.get('rating') or '-'} stars. Volume is compared with about {tgt} reviews"
                 + (" (the typical business in this search)." if r.typical else ".")
                 + " Reply to every review and ask recent customers for one.", comp_rev))
    hours_ok = ex.get("hours") == "Yes" and a.get("phone") and a.get("website")
    gaps.append(("Hours & contact", "OK" if hours_ok else "MISSING" if ex.get("hours") == "No" else "PARTIAL",
                 f"Phone: {a.get('phone') or 'missing'}. Website: {'listed' if a.get('website') else 'missing'}. Hours: "
                 + {"Yes": "listed.", "No": "missing."}.get(ex.get("hours"), "not shown on the page.")
                 + " Keep hours current and add holiday hours.", comp_site))
    gaps.append(("Website & citations",
                 "NOT MEASURED" if not site_ok else "PARTIAL" if (nap and nap["ok"] is False) or not prof else "OK",
                 web_txt + " Apple, Bing and directory listings were not checked.", DASH))
    rows, style = [], []
    for i, (el, st_, gap, cp) in enumerate(gaps, 1):
        cell, col = _status_cell(st_)
        rows.append([P(el, CELLB), cell, P(gap), P(cp)])
        style += [("BACKGROUND", (1, i), (1, i), col), ("VALIGN", (1, i), (1, i), "MIDDLE")]
    story += [table(["Element", "Status", "Gap analysis", "What competitors do better"], rows,
                    [S(30), S(24), S(66), W - S(120)], extra_style=style)]
    notes = ["Competitor figures come from the other listings in the same Maps search."]
    if r.not_captured:
        notes.append("Not shown on the Google Maps page, so not included: " + ", ".join(r.not_captured) +
                     ". Add them from GMB Everywhere on the Audit page and they appear in the next report.")
    story += [Spacer(1, 3), Paragraph(" ".join(notes), NOTE)]

    # ---- 4 keywords
    story += heading(4, "Keyword Targeting Audit")
    c0 = (cat or "local business").lower()
    tl = next((c for c in a["checks"] if c["id"] == "title_local"), None)
    title_sig = ("Homepage title and headings mention " + r.area + "." if tl and tl["ok"] else
                 "Homepage title and headings do not mention " + r.area + "." if tl and tl["ok"] is False else
                 "Homepage could not be checked.")
    kws = []
    if r.q:
        kws.append((r.q, "Transactional (local)",
                    f"The search run for this audit: position #{r.rank} of {r.total}." if r.rank else "The search run for this audit."))
    kws += [(f"{c0} {r.area}", "Transactional (local)", title_sig),
            (f"{c0} near me", "Transactional (map pack)", "Proximity-driven: depends on profile completeness and reviews."),
            (f"best {c0} in {r.area}", "Commercial investigation", f"Driven by reviews: {_num(r.reviews)} at {facts.get('rating') or '-'} stars."),
            (f"emergency {c0} {r.area}", "Urgent / transactional",
             "Only worth targeting if offered: needs a matching service and hours." + (" Hours are listed." if ex.get("hours") == "Yes" else "")),
            (f"{c0} {r.area} reviews", "Reputation", "Review volume and owner replies.")]
    seen, rows = set(), []
    for k, i, sig in kws:
        if k.lower() not in seen:
            seen.add(k.lower())
            rows.append([P(k, CELLB), P(i), P(sig)])
    story += [P("Target terms come from the profile category, the area and the search that was run.", BODY), Spacer(1, 4)]
    story += [table(["Keyword", "Intent", "Signal on this business"], rows, [S(52), S(40), W - S(92)])]
    story += [Spacer(1, 3), Paragraph("Search volume and rankings by area were not measured. Check Google Keyword Planner and run a "
                                      "map-pack scan before writing content briefs.", NOTE)]
    return story


# ======================================================================= sections 5-12
def rest_story(r):
    a, ex, facts = r.a, r.extra, r.facts
    story, lp = [], r.lp

    # ---- 5 reviews
    story += heading(5, "Review Management Assessment")
    if r.rating is not None and r.rating < 4.3:
        gs = "NEEDS WORK"
    elif r.reviews is not None and r.reviews < r.tgt:
        gs = "GOOD (quality) / LOW volume" if r.rating and r.rating >= 4.5 else "LOW volume"
    else:
        gs = "GOOD"
    story += [table(["Platform", "Review count", "Avg rating", "Status"], [
        [P("Google Business Profile", CELLB), P(_num(r.reviews)), P(facts.get("rating") or "-"), stat(gs)]],
        [S(52), S(34), S(30), W - S(116)])]
    story += [Spacer(1, 3), Paragraph("Reviews on Facebook, Trustpilot and Yelp, and the owner response rate, were not collected.", NOTE)]
    best = r.best[0] if r.best else None
    if best and r.reviews is not None and best["reviews"] > r.reviews:
        gap = f"In this search, {best['name']} has {best['reviews']:,} reviews against {r.reviews:,}."
    else:
        gap = "Compared with the other businesses in this search, review volume is not the main gap."
    story += [callout("Key finding", [
        (f"A {facts.get('rating')} rating is a genuine asset" if r.rating and r.rating >= 4.5 else f"The rating is {facts.get('rating') or 'unknown'}")
        + (f" but it is attached to only {r.reviews:,} reviews. " if r.reviews is not None and r.reviews < r.tgt else f", with {_num(r.reviews)} reviews. ")
        + gap,
        "Review volume, recency and owner replies feed both the map pack ranking and click-through. Put the Google review "
        "link in a text sent within an hour of finishing each job."])]

    # ---- 6 posts
    story += heading(6, "GMB Post Activity")
    if lp is None:
        story += [P("The date of the last post could not be read from the Maps page, so posting is not scored. Check the "
                    "profile's Updates tab.", BODY)]
    else:
        now = ("No posts ever." if lp >= 99 else f"Last post about {_months(lp)} ago." if lp else "Last post this month.")
        story += [P("Current status: " + now, BODY)]
    story += [Spacer(1, 4), table(["Recommended post type", "Cadence", "Impact on rankings"], [
        [P("What's New updates", CELLB), P("Weekly to fortnightly"), P("Freshness signal for Google; an idle profile looks abandoned beside active rivals.")],
        [P("Offers", CELLB), P("Seasonal"), P("Drives clicks and calls; supports \"near me\" intent.")],
        [P("Service posts", CELLB), P("Monthly"), P("Reinforces service keywords and links to landing pages.")],
        [P("Before/after and team posts", CELLB), P("Monthly"), P("Builds trust and feeds the photo library.")],
    ], [S(46), S(40), W - S(86)])]
    story += [callout("Key finding", [
        "Posting is not scored because its date was not captured." if lp is None else
        f"Posting is {'inactive' if lp >= 2 else 'active'}. The fix is cheap: one 100-150 word post per week, built from real jobs, "
        "each with a photo and a call-to-action button."])]

    # ---- 7 citations
    story += heading(7, "Citation Building Analysis")
    nap = next((c for c in a["checks"] if c["id"] == "nap"), None)
    nap_txt = ("Checked: the Google listing phone number is on the homepage." if nap and nap["ok"] else
               "Checked: the Google listing phone number was not found on the homepage." if nap and nap["ok"] is False else
               "The homepage could not be compared with the listing.")
    base_name = f"{a['company']}, {a.get('address') or ''}, {a.get('phone') or ''}"
    rows = [[P("Google Business Profile", CELLB), stat("CRITICAL"), P("Baseline NAP: " + base_name)],
            [P("Company website (homepage)", CELLB), stat("IMPORTANT"), P(nap_txt)]]
    for name, prio in [("Apple Business Connect / Maps", "IMPORTANT"), ("Bing Places", "IMPORTANT"), ("Facebook Business Page", "IMPORTANT"),
                       ("Yelp / Trustpilot", "ENHANCE"), ("Local business directories", "ENHANCE")]:
        rows.append([P(name, CELLB), stat(prio), P("Claim or correct it with exactly the same name, address and phone.")])
    story += [table(["Directory", "Priority", "Action"], rows, [S(52), S(26), W - S(78)])]
    story += [Spacer(1, 3), Paragraph("External directories were not checked, so their current state is not reported.", NOTE)]
    story += [callout("NAP rule", [
        "Decide on one name, address and phone format and use it exactly on every listing, including legal suffixes and the "
        "phone format. Use the clean website domain without tracking tags."], color=ORANGE)]

    # ---- 8 map pack
    story += heading(8, "Map Pack Ranking")
    if r.rank:
        story += [P(f"When this search was run (\"{r.q}\"), this business appeared at position #{r.rank} of {r.total} in the Google Maps "
                    "list. That position depends on where the search is made from, so treat it as indicative only.", BODY)]
    else:
        story += [P("The position in the Maps list was not captured for this business.", BODY)]
    story += [callout("Next step", [
        "A geo-grid scan (map-pack positions across the areas you serve) gives the baseline every later result is measured "
        "against. It is a Week 1 action in Section 10, and no timelines are promised before it exists."], color=ORANGE)]

    # ---- 9 competitors
    story += heading(9, "Competitor Landscape")
    top5 = _top_of_search(r.comps, 5)
    if top5:
        story += [P("The businesses ranked first in the same Google Maps search. Figures come from their listings; nothing is "
                    "estimated.", BODY), Spacer(1, 4)]
        rows = []
        for c in top5:
            diff = ("" if r.reviews is None or c["reviews"] is None else
                    f"{c['reviews'] - r.reviews:+,} reviews" + ("" if r.rating is None or c["rating"] is None else f", {c['rating'] - r.rating:+.1f} rating"))
            rows.append([P(f"#{c['rank']}"), P(c["name"], CELLB), P(c["rating"] if c["rating"] is not None else "-"),
                         P(_num(c["reviews"])), P(c.get("category") or "-"), P(diff or "-")])
        story += [table(["Pos.", "Competitor", "Rating", "Reviews", "Category", "Gap vs this business"], rows,
                        [S(11), S(46), S(15), S(18), S(38), W - S(128)])]
        ahead = sum(1 for c in top5 if r.reviews is not None and (c["reviews"] or 0) > r.reviews)
        story += [callout("Reading the table", [
            f"{ahead} of the top {len(top5)} results have more reviews than {a['company']}."
            + (f" The most reviewed business in this search, {best['name']}, has {best['reviews']:,}." if best else ""),
            "Review count and recency are the clearest gap that can be closed in a quarter. Authority, press and brand "
            "history take longer, so compete first on proximity and review freshness."])]
    else:
        story += [P("No other businesses with review data were available from this search.", BODY)]

    # ---- 10 action plan
    story += heading(10, "Priority Action Plan")
    rows = []
    for i, x in enumerate(r.acts, 1):
        rows.append([P(str(i)), [Paragraph(f"<b>{tx(x['title'])}</b>", CELL), Paragraph(tx(x["desc"]), _style("d", fontSize=7.6, leading=9.8, textColor=GREY))],
                     stat(x["sev"]), P(x["timeline"]), P(x["effort"])])
    story += [table(["#", "Action", "Severity", "Timeline", "Effort"], rows, [S(8), W - S(8 + 26 + 22 + 18), S(26), S(22), S(18)])]

    # ---- 11 roadmap
    story += heading(11, "90-Day Roadmap")
    phases = {1: ("WEEK 1-2", "Foundations"), 2: ("WEEK 3-4", "Profile build-out"), 3: ("MONTH 2", "Authority and relevance"),
              4: ("MONTH 3", "Expand and prove")}
    todos = {k: [] for k in phases}
    for x in r.acts:
        todos[x["phase"]].append(x["todo"])
    todos[1].insert(0, "Capture a geo-grid baseline for the core keywords")
    todos[1] = list(dict.fromkeys(todos[1]))
    todos[4] += ["Re-run the geo-grid scan and compare with the baseline", "Review GBP Insights (calls, directions, website clicks) and set next-quarter targets"]
    for k, (when, what) in phases.items():
        items = [Paragraph(f'<font color="{_hex(TEAL)}"><b>[&nbsp;&nbsp;]</b></font>&nbsp;&nbsp;{tx(t)}', BODY) for t in todos[k]]
        story += [KeepTogether([Paragraph(f'<font color="{_hex(TEAL)}"><b>{when}</b></font>&nbsp;|&nbsp;<b>{what}</b>', H2)] + items)]
    story += [callout("Expected results at 90 days", [
        "These are targets that follow from completing the plan, not guarantees.",
        *([f"•  Review count grown from {r.reviews} toward {max(r.reviews + 15, round(r.reviews * 1.6))}+ with every review answered."] if r.reviews is not None else []),
        "•  12+ consecutive weekly posts live, and the profile complete (categories, services, 25+ photos).",
        "•  Core citations live and consistent across at least 6 directories.",
        "•  Map pack positions improved against the Week 1 geo-grid baseline.",
        "•  A measurable lift in calls and direction requests in GBP Insights."])]

    # ---- 12 executive summary
    story += heading(12, "Executive Summary")
    crit = [x["title"][0].lower() + x["title"][1:] for x in r.acts if x["sev"] == "CRITICAL"][:3]
    story += [P(f"{a['company']} already has " + (", ".join(r.strengths) if r.strengths else "a live Google listing and website") + ".", BODY),
              Spacer(1, 4),
              P(("The first priorities are to " + ", ".join(crit) if crit else "No critical gaps were confirmed") +
                ". The plan above is ordered by impact and is built to be completed within 90 days.", BODY)]
    return story


# ======================================================================= outputs
class _PageNumbers(canvas.Canvas):
    """'Page X of Y' bottom right, drawn after the content of every page."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Helvetica", 6.8)
            self.setFillColor(GREY)
            self.drawRightString(PW - PW * SIDE, 20, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _top_margin(pw):
    return PAD_RULE_ROW * pw / PAD_W + 16


def build_pdf(a, extra, others, settings, query="", rank=None, total=None):
    """The full report: 12 sections, several A4 pages on the pad, no blank gaps. Returns PDF bytes."""
    with _LOCK:
        _layout(A4[0], 35 * mm)
        try:
            r = _prepare(a, extra, others, settings, query, rank, total)
            story = short_story(r) + rest_story(r)
            buf = BytesIO()
            side = PW * SIDE
            doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=side, rightMargin=side, topMargin=_top_margin(PW),
                                  bottomMargin=46, title=f"Local SEO Audit - {a['company']}",
                                  author=r.biz or "Local SEO audit")
            doc.addPageTemplates([PageTemplate(id="p", onPage=lambda c, d: draw_pad(c, A4[0], A4[1]),
                                               frames=[Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                                                             leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])])
            doc.build(story, canvasmaker=_PageNumbers)
            return buf.getvalue()
        finally:
            _layout(A4[0])


class _End(Flowable):
    """Zero-size marker that records where the content ended."""

    def __init__(self, box):
        super().__init__()
        self.box = box

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        self.box["y"] = self.canv.absolutePosition(0, 0)[1]


IMG_PW = 450          # layout width (pt) of the email image: narrower than A4, so the text reads larger
IMG_PX = 1400         # pixel width of the JPG
IMG_BOTTOM = 118      # room kept for the pad's footer artwork


def _tall_pdf(r_args):
    """One very tall page: the pad's top artwork, the four sections, the pad's footer artwork."""
    side = PW * SIDE
    top = _top_margin(PW)
    big = 40000
    box = {}
    r = _prepare(*r_args)
    story = short_story(r) + [_End(box)]
    doc = BaseDocTemplate(BytesIO(), pagesize=(PW, big), leftMargin=side, rightMargin=side, topMargin=top, bottomMargin=0)
    doc.addPageTemplates([PageTemplate(id="m", frames=[Frame(side, 0, W, big - top, leftPadding=0, rightPadding=0,
                                                              topPadding=0, bottomPadding=0)])])
    doc.build(story)
    content_h = (big - top) - box["y"]
    height = top + content_h + IMG_BOTTOM + 4

    story = short_story(_prepare(*r_args))
    buf = BytesIO()
    doc = BaseDocTemplate(buf, pagesize=(PW, height), leftMargin=side, rightMargin=side, topMargin=top,
                          bottomMargin=IMG_BOTTOM, title=f"Local SEO Audit - {r.a['company']}")
    doc.addPageTemplates([PageTemplate(id="t", onPage=lambda c, d: draw_pad(c, PW, height),
                                       frames=[Frame(side, IMG_BOTTOM, W, height - top - IMG_BOTTOM, leftPadding=0,
                                                     rightPadding=0, topPadding=0, bottomPadding=0)])])
    doc.build(story)
    return buf.getvalue()


def _pdf_to_jpg(pdf_bytes, px_width=IMG_PX, quality=86):
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("The report image needs the 'pypdfium2' package. Run: pip install pypdfium2") from e
    doc = pdfium.PdfDocument(pdf_bytes)
    page = doc[0]
    scale = px_width / page.get_width()
    img = page.render(scale=scale).to_pil().convert("RGB")
    out = BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def build_image(a, extra, others, settings, query="", rank=None, total=None):
    """The short report (cover + sections 1-4) as ONE tall JPG on the pad. Returns JPEG bytes."""
    with _LOCK:
        _layout(IMG_PW, 0)
        try:
            pdf = _tall_pdf((a, extra, others, settings, query, rank, total))
        finally:
            _layout(A4[0])
    return _pdf_to_jpg(pdf)