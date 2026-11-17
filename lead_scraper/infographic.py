"""
infographic.py - the one-image "Free Local Visibility Snapshot" that goes inline in the email.

It is an HTML page (720 px wide, Starkology look) turned into a JPG with the Chromium that Playwright already
installed for the scraper. The story is chosen from the audit data: only things that were really measured are
used, and blocks without data are left out.

    build_infographic(a, extra, others, settings, query, rank, total) -> JPEG bytes
"""

import base64
import os
import re
import subprocess
import sys
import tempfile
from html import escape
from io import BytesIO
from pathlib import Path

import report
from audit import _to_float

ASSETS = Path(__file__).parent / "assets"
FONT_DIR = ASSETS / "fonts"
PAD_FILE = ASSETS / "pad.png"
WIDTH = 720

NAVY, TEAL, MINT, INK, MUTED, GREYBG = "#12304F", "#17C3B2", "#E8F9F6", "#1B2A3A", "#51606F", "#F4F7FA"
TEAL_TEXT = "#0E7490"  # teal that is readable on white
STATUS = {"STRONG": "#1E7B45", "GOOD": "#0E7490", "NEEDS WORK": "#B45309", "CRITICAL": "#B3261E"}

LABEL = {"GBP Optimization": "Google profile", "Keyword Targeting": "Search keywords",
         "Citation Strength": "Business details match", "Review Management": "Customer reviews",
         "GMB Post Activity": "Weekly posts", "Website / SEO": "Website"}
WIN = {"Google profile": "Your Google profile is in good shape.", "Search keywords": "Your site speaks the local search language.",
       "Business details match": "Your business details are consistent.", "Customer reviews": "Customers trust you.",
       "Weekly posts": "You post regularly.", "Website": "Your website is solid."}
HELD = {"Google profile": "how complete your Google profile is", "Search keywords": "how your website talks to Google",
        "Business details match": "how consistent your business details are online", "Customer reviews": "your reviews",
        "Weekly posts": "how rarely you post", "Website": "your website"}
VERDICT = {"STRONG": "A strong position. Keep it up.", "GOOD": "A good base. Easy to lift.",
           "NEEDS WORK": "Real gaps, and they are fixable.", "CRITICAL": "Big gaps, big upside."}

# short, plain-words version of each action in the plan, and the theme of its column
PLAN_LINE = {
    "Restart GBP posting cadence": ("Start weekly posts", "Start posting"),
    "Check posting cadence and set a weekly routine": ("Check how often you post", "Start posting"),
    "Launch a review-generation system": ("Ask every happy customer for a review", "Win more reviews"),
    "Recover the rating": ("Reply to every critical review", "Win more reviews"),
    "Add secondary categories and attributes": ("Add the right extra categories", "Complete your profile"),
    "Rebuild the services list and description": ("Rewrite your description and services", "Complete your profile"),
    "Upload a photo set": ("Add 25+ real photos", "Complete your profile"),
    "Audit the live profile for completeness": ("Complete your Google profile", "Complete your profile"),
    "Capture a geo-grid baseline": ("Measure where you show up across the city", "Check the basics"),
    "Fix the website security certificate": ("Fix the security warning", "Fix your website"),
    "Move the website to HTTPS": ("Switch the site to secure HTTPS", "Fix your website"),
    "Make the website mobile-friendly": ("Make the site work on phones", "Fix your website"),
    "Rewrite title, headings and description for local search": ("Add your city to the title and main heading", "Fix the homepage"),
    "Create service and area landing pages": ("One page per service and area", "Build local pages"),
    "Add LocalBusiness schema": ("Add the business details Google reads", "Build local pages"),
    "Website tidy-up": ("Small website fixes", "Fix your website"),
    "Check the website by hand": ("Check the website", "Fix your website"),
    "Lock the NAP and build core citations": ("Match your details on Apple, Bing and Facebook", "Match your details"),
    "Earn local links and mentions": ("Get local links and mentions", "Build local pages"),
}

ICONS = {
    "pin": '<path d="M12 21s7-6.1 7-11.5A7 7 0 0 0 5 9.5C5 14.9 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
    "star": '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/>',
    "lock": '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "phone": '<rect x="7" y="2.5" width="10" height="19" rx="2"/><path d="M11 18.5h2"/>',
    "doc": '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 16h5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
}


def icon(name, size=28, color="currentColor"):
    return (f'<svg aria-hidden="true" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')


def esc(s):
    return escape(str(s if s is not None else ""), quote=True)


def band_name(score):
    return "CRITICAL" if score < 25 else "NEEDS WORK" if score < 50 else "GOOD" if score <= 75 else "STRONG"


# ------------------------------------------------------------------- assets
def _b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def _fonts_css():
    css = []
    for w in (400, 500, 600, 700, 800):
        f = FONT_DIR / f"plus-jakarta-sans-latin-{w}-normal.woff2"
        if f.exists():
            css.append("@font-face{font-family:'Plus Jakarta Sans';font-weight:%d;font-style:normal;"
                       "src:url(data:font/woff2;base64,%s) format('woff2')}" % (w, _b64(f)))
    return "".join(css)


HEADER_FILE = ASSETS / "header.png"


def _header_png():
    """The Starkology header. assets/header.png is used as it is; if it is missing, one is cut out of the pad."""
    from PIL import Image
    if HEADER_FILE.exists():
        im = Image.open(HEADER_FILE).convert("RGBA")
        flat = Image.new("RGB", im.size, "white")
        flat.paste(im, mask=im.split()[3])
        out = BytesIO()
        flat.save(out, "PNG", optimize=True)
        return out.getvalue()
    im = Image.open(PAD_FILE).convert("RGBA")
    bg = Image.new("RGB", im.size, "white")
    bg.paste(im, mask=im.split()[3])
    crop = bg.crop((0, 52, im.size[0], 272))
    px = crop.load()
    for y in range(crop.height):
        for x in range(crop.width):
            r, g, b = px[x, y]
            if g >= 250 and r >= 230 and b >= 242 and abs(r - b) < 24 and max(r, g, b) - min(r, g, b) < 28:
                px[x, y] = (255, 255, 255)   # the faint mint shapes behind the logo
    out = BytesIO()
    crop.save(out, "PNG", optimize=True)
    return out.getvalue()


# ----------------------------------------------------------------- the story
def _short(name, n=34):
    name = " ".join((name or "").split())
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def _plain_cat(r):
    return (r.cat or "").strip().lower() or "business"


def _story(r):
    """Everything the page says, as plain data, using only facts that were measured."""
    a, cb, ex = r.a, r.cb, r.extra
    reviews, rating, rank, total = r.reviews, r.rating, r.rank, r.total
    area = r.area if r.area != "Local area" else ""
    by_rank = {c["rank"]: c for c in r.comps if c.get("rank") and c.get("name")}
    c1, c3 = by_rank.get(1), by_rank.get(3)
    ahead = [c for c in sorted(by_rank.values(), key=lambda c: c["rank"]) if rank and c["rank"] < rank][:3]
    top_names = [c["name"] for c in ahead[:2]]
    behind_top3 = bool(rank and rank > 3 and len(top_names) >= 2)
    q = r.q
    s = {"c1": c1, "c3": c3}

    web_bad = any(cb.get(i) is False for i in ("title_local", "schema", "https", "ssl_invalid", "mobile"))
    outreview = bool(c1 and c1.get("reviews") is not None and reviews is not None and reviews > c1["reviews"])

    # ---- headline
    if outreview and web_bad:
        head = f"You out-review the [[g:#1 {_plain_cat(r)}]] on Google Maps. Your website isn't helping."
    elif behind_top3:
        head = (f"[[g:{_short(top_names[0], 28)}]] and [[g:{_short(top_names[1], 28)}]] are in the top 3 on Google Maps. "
                f"[[r:You're #{rank}.]]")
    elif cb.get("rating_low") is False and rating is not None:
        head = f"Your {rating:g}-star rating is costing you calls."
    elif reviews is not None and reviews < r.tgt:
        head = "Your Google listing has fewer reviews than the businesses ahead of you."
    else:
        head = "Your Google listing has room to win more calls."
    s["headline"] = head

    # ---- hero pills (strongest real facts)
    pills = []
    if rank and total:
        pills.append((f"#{rank} of {total} on Google Maps", "r"))   # the company itself: red
    if rating is not None and reviews is not None:
        pills.append((f"{rating:g} rating · {reviews:,} reviews", ""))
    if r.sc["overall"] is not None:
        pills.append((f"{r.sc['overall']}% overall score", ""))
    s["pills"] = pills[:3]
    # the other businesses it is compared with: green
    others = [c for c in sorted(by_rank.values(), key=lambda c: c["rank"]) if c["rank"] != rank][:3]
    s["tops"] = [(f"#{c['rank']} {_short(c['name'], 30)}", "g") for c in others] if rank else []

    # ---- score block
    cats = [(LABEL.get(n, n), sc, band_name(sc)) for n, sc, _ in r.sc["cats"] if sc is not None]
    cats.sort(key=lambda t: -t[1])
    s["cats"] = cats
    overall = r.sc["overall"]
    if overall is not None and cats:
        strong, weak = cats[0], cats[-1]
        if strong[1] >= 50 and weak[1] < strong[1] and len(cats) > 1:
            line = f"{WIN[strong[0]]} The score is held back by {HELD[weak[0]]}."
        elif len(cats) > 1 and weak[1] < 50:
            line = f"The biggest gap is {HELD[weak[0]]}."
        else:
            line = "Every area we measured is in good shape."
        s["score"] = {"value": overall, "band": band_name(overall), "verdict": VERDICT[band_name(overall)], "line": line}

    # ---- strengths strip
    # (rating and review count are already in the hero pills, so they are not repeated here)
    wins = []
    if rank and rank <= 3:
        wins.append(f"#{rank} on Google Maps")
    if cb.get("https") and cb.get("mobile"):
        wins.append("Secure, mobile-friendly website")
    if ex.get("claimed") == "Yes":
        wins.append("Claimed Google profile")
    if ex.get("hours") == "Yes":
        wins.append("Opening hours listed")
    if ex.get("description") == "Yes":
        wins.append("Business description set")
    if cb.get("schema") is True:
        wins.append("Local business markup on your site")
    s["wins"] = wins if len(wins) >= 4 else []

    # ---- leaks, biggest first
    leaks = []

    def leak(key, ic, title, fact, cost):
        leaks.append({"key": key, "icon": ic, "title": title, "fact": fact, "cost": cost})

    if behind_top3:
        names = " and ".join(_short(n, 30) for n in top_names)
        leak("rank", "pin", f"You're #{rank} for \"{q}\"" if q else f"You're #{rank} on Google Maps",
             f"{names} are ahead of you in the top 3.", f"people searching{(' \"' + q + '\"') if q else ''} call the top 3 first.")
    if cb.get("rating_low") is False and rating is not None:
        leak("rating", "star", f"Your {rating:g}-star rating puts people off",
             "Google shows your rating next to your name in every search.", "calls go to higher-rated businesses nearby.")
    if reviews is not None and (cb.get("reviews_low") is False or reviews < r.tgt):
        more = (f"{_short(c1['name'], 30)} has {c1['reviews']:,}." if c1 and c1.get("reviews") and c1["reviews"] > reviews
                else "Review count is both a trust signal and a ranking signal.")
        leak("reviews", "star", f"Only {reviews:,} review{'' if reviews == 1 else 's'}", more,
             "fewer reviews means less trust and a weaker place in the map results.")
    if cb.get("ssl_invalid") is False:
        leak("ssl", "lock", "Visitors see a security warning", "Your website's security certificate looks invalid.",
             "some visitors leave before they ever call.")
    elif cb.get("https") is False:
        leak("https", "lock", 'Chrome says your website is "Not secure"', "The site loads without a secure connection.",
             "some visitors leave before they ever call.")
    if cb.get("mobile") is False:
        leak("mobile", "phone", "Your website isn't set up for phones", "Most local searches happen on a phone.",
             "phone visitors leave instead of calling.")
    if ex.get("claimed") == "No":
        leak("claimed", "shield", "Your Google profile isn't claimed", "Nobody at the business controls what Google shows.",
             "you can't reply to reviews or fix your details.")
    if cb.get("title_local") is False and area:
        leak("title", "pin", f"Your homepage never says \"{area}\"",
             f"{area} is missing from your homepage title and main headings, so Google has less reason to show you for local searches.",
             f"people searching \"{_plain_cat(r)} in {area}\" see others first.")
    if cb.get("schema") is False:
        leak("schema", "doc", "Google can't read your business details",
             "Your site has no local business markup, the hidden label that tells Google your name, address, phone and services.",
             "a weaker match between your website and your Google profile.")
    if r.lp is not None and r.lp >= 3:
        leak("posts", "clock", "Your profile looks inactive",
             "You have never posted on your profile." if r.lp >= 99 else f"Your last post was about {r.lp} months ago.",
             "a quiet profile looks abandoned next to active competitors.")
    if ex.get("hours") == "No":
        leak("hours", "clock", "No opening hours on your profile", "People can't tell whether you are open.",
             "some will pick a business that shows its hours.")
    if ex.get("description") == "No":
        leak("desc", "doc", "No business description", "The description is where you tell Google what you do and where.",
             "less for Google to match you to local searches.")
    if cb.get("nap") is False:
        leak("nap", "phone", "Your phone number isn't on your homepage", "The number on your Google profile can't be found on your website.",
             "Google and customers see two different stories.")
    if cb.get("tel_link") is False:
        leak("tel", "phone", "No tap-to-call button", "People on a phone have to copy your number.", "easy calls turn into lost calls.")
    s["leaks"] = leaks[:4]

    # ---- opportunity
    stats = []
    if c1 and c1.get("reviews") is not None and reviews is not None:
        stats.append((f"{reviews:,} vs {c1['reviews']:,}", "reviews: you vs the #1 result"))
    if c3 and c3.get("reviews") is not None and reviews is not None:
        stats.append((f"{reviews:,} vs {c3['reviews']:,}", "reviews: you vs the #3 result"))
    if rank and rank > 1:
        stats.append((f"#{rank}", "today, with room to climb"))
    if len(stats) >= 2:
        if outreview and leaks:
            note = "You already have the reviews. Fixing the gaps above is the fastest way to turn them into rank."
        elif c1 and c1.get("reviews") and reviews is not None and reviews < c1["reviews"]:
            note = "The review gap is the clearest one to close, and every fix in this plan supports it."
        else:
            note = f"These are the fixes that move you up from #{rank}." if rank else "These are the fixes that move you up."
        s["opp"] = {"stats": stats[:3], "note": note}
    else:
        s["opp"] = None
        wins_easy = [x for x in r.acts if x["effort"] == "Low" and x["title"] in PLAN_LINE][:3]
        s["easy"] = [PLAN_LINE[x["title"]][0] for x in wins_easy]

    # ---- action plan (3 columns)
    cols = {1: ("WEEKS 1-2", []), 2: ("WEEKS 3-4", []), 3: ("MONTH 2-3", [])}
    for x in r.acts:
        if x["title"] not in PLAN_LINE:
            continue
        ph = min(x["phase"], 3)
        line, theme = PLAN_LINE[x["title"]]
        if line == "Add your city to the title and main heading" and area:
            line = f"Add \"{area}\" to the title and main heading"
        cols[ph][1].append((line, theme))
    plan = []
    for k in (1, 2, 3):
        label, items = cols[k]
        if items:
            plan.append({"label": label, "title": items[0][1], "lines": [i[0] for i in items[:3]]})
    s["plan"] = plan
    return s


# ---------------------------------------------------------------------- html
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#fff;font-family:'Plus Jakarta Sans',Arial,sans-serif;color:%(ink)s;-webkit-font-smoothing:antialiased}
#root{position:relative;width:720px;overflow:hidden;background:#fff;padding-bottom:48px}
.glow{position:absolute;width:660px;height:660px;border-radius:50%%;pointer-events:none;mix-blend-mode:multiply;z-index:0;opacity:.55}
.glow.g{background:radial-gradient(circle,rgba(120,240,196,.9) 0%%,rgba(120,240,196,0) 68%%)}
.glow.c{background:radial-gradient(circle,rgba(110,226,240,.9) 0%%,rgba(110,226,240,0) 68%%)}
.content{position:relative;z-index:1}
.topline{height:2px;background:#9EEBE3;margin:0 0 0 0}
.hdr{display:block;width:720px;height:auto}
.block{margin:0 48px}
.sec{margin-top:56px}
.hero{margin-top:48px;background:%(navy)s;border-radius:20px;padding:40px}
.eyebrow{font-size:12px;font-weight:800;letter-spacing:.14em;color:%(teal)s;text-transform:uppercase}
.hero h1{font-size:36px;line-height:1.14;font-weight:800;color:#fff;margin-top:14px;letter-spacing:-.01em}
.prep{font-size:18px;font-weight:700;color:#fff;margin-top:18px}
.date{font-size:14px;color:#C9D6E3;margin-top:4px}
.pills{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}
.hr{color:#FF7A7A}.hg{color:#4FE3A1}
.pills.tops{margin-top:10px;align-items:center}.plabel{font-size:13px;font-weight:600;color:rgba(255,255,255,.65);margin-right:2px}
.pill.r{border-color:#FF7A7A;background:rgba(255,90,90,.18);color:#FFB3B3}
.pill.g{border-color:#4FE3A1;background:rgba(79,227,161,.14);color:#A8F2CE}
.pill{font-size:14px;font-weight:600;color:#fff;border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.08);border-radius:999px;padding:8px 16px}
.h{display:flex;align-items:center;gap:14px}
.badge{width:40px;height:40px;border-radius:50%%;background:%(teal)s;color:#fff;font-weight:800;font-size:18px;display:flex;align-items:center;justify-content:center;flex:none}
.h h2{font-size:28px;font-weight:800;color:%(navy)s;letter-spacing:-.01em}
.card{background:rgba(232,249,246,.9);border-radius:20px;padding:32px;margin-top:20px}
.scoretop{display:flex;gap:28px;align-items:center}
.verdict{font-size:22px;font-weight:800;color:%(tt)s}
.vline{font-size:16px;line-height:1.5;color:%(muted)s;margin-top:8px}
.rows{margin-top:26px}
.row{display:flex;align-items:center;gap:14px;margin-top:12px;font-size:16px;font-weight:500}
.row .l{width:178px;flex:none}
.bar{flex:1;height:14px;border-radius:7px;background:#E3E9EF;overflow:hidden}
.bar i{display:block;height:100%%;border-radius:7px}
.row .v{width:46px;text-align:right;font-weight:600}
.chip{width:104px;text-align:center;font-size:12px;font-weight:800;color:#fff;border-radius:999px;padding:4px 0;flex:none}
.leak{border:2px solid #E6ECF2;border-radius:18px;padding:24px;margin-top:18px;background:rgba(255,255,255,.88);display:flex;gap:20px}
.tile{width:56px;height:56px;border-radius:14px;background:#FDEFE0;color:#B45309;display:flex;align-items:center;justify-content:center;flex:none}
.leak .lab{font-size:12px;font-weight:800;letter-spacing:.1em;color:#B45309}
.leak h3{font-size:21px;font-weight:800;color:%(navy)s;margin-top:4px;line-height:1.25}
.leak p{font-size:16px;line-height:1.5;color:%(muted)s;margin-top:6px}
.leak .cost{font-weight:700;color:#B3261E;margin-top:8px}
.stats{display:flex;gap:14px;margin-top:20px}
.stat{flex:1;background:rgba(232,249,246,.92);border-radius:16px;padding:22px 12px;text-align:center}
.stat b{display:block;font-size:34px;font-weight:800;color:%(navy)s;letter-spacing:-.01em}
.stat span{display:block;font-size:16px;line-height:1.35;color:%(muted)s;margin-top:6px}
.note{font-size:16px;line-height:1.5;color:%(ink)s;margin-top:16px;font-weight:500}
.chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
.win{display:flex;align-items:center;gap:8px;background:rgba(232,249,246,.92);border-radius:999px;padding:10px 16px;font-size:16px;font-weight:600;color:%(navy)s}
.win svg{color:#1E7B45}
.plan{display:flex;gap:14px;margin-top:20px;align-items:stretch}
.col{flex:1;background:%(grey)s;border-top:6px solid %(teal)s;border-radius:16px;padding:20px 18px 22px}
.col .when{font-size:12px;font-weight:800;letter-spacing:.1em;color:%(tt)s}
.col h4{font-size:20px;font-weight:800;color:%(navy)s;margin-top:6px;line-height:1.25}
.col p{font-size:16px;line-height:1.4;color:%(muted)s;margin-top:10px}
.cta{margin:56px 48px 0;background:%(navy)s;border-radius:20px;padding:36px 40px}
.cta h2{font-size:32px;font-weight:800;color:#fff;margin-top:10px;letter-spacing:-.01em}
.cta p{font-size:16px;line-height:1.5;color:#D6E0EA;margin-top:12px}
""" % {"ink": INK, "navy": NAVY, "teal": TEAL, "tt": TEAL_TEXT, "muted": MUTED, "grey": GREYBG}


def _ring(value, band):
    col = STATUS[band]
    dash = max(0, min(100, value)) * 3.896
    return (f'<svg aria-hidden="true" width="150" height="150" viewBox="0 0 150 150" style="flex:none">'
            f'<circle cx="75" cy="75" r="62" fill="none" stroke="#CFEDE8" stroke-width="14"/>'
            f'<circle cx="75" cy="75" r="62" fill="none" stroke="{col}" stroke-width="14" stroke-linecap="round" '
            f'stroke-dasharray="{dash:.1f} 389.6" transform="rotate(-90 75 75)"/>'
            f'<text x="75" y="84" text-anchor="middle" font-family="Plus Jakarta Sans, Arial" font-size="32" font-weight="800" '
            f'fill="{NAVY}">{value}%</text></svg>')


def render_html(r, s):
    parts, n = [], 0

    def head(title):
        nonlocal n
        n += 1
        return f'<div class="h"><div class="badge">{n}</div><h2>{esc(title)}</h2></div>'

    def pill_row(items):
        return "".join(f'<span class="pill {k}">{esc(t)}</span>' for t, k in items)

    def mark(text):
        """[[r:text]] -> red, [[g:text]] -> green; everything else plain. All text is escaped."""
        out_, pos = [], 0
        for m in re.finditer(r"\[\[([rg]):(.*?)\]\]", text):
            out_.append(esc(text[pos:m.start()]))
            out_.append(f'<span class="h{m.group(1)}">{esc(m.group(2))}</span>')
            pos = m.end()
        out_.append(esc(text[pos:]))
        return "".join(out_)

    pills = pill_row(s["pills"])
    if s.get("tops"):
        pills += '</div><div class="pills tops"><span class="plabel">Also in this search</span>' + pill_row(s["tops"])
    prep = esc(r.a["company"]) + (f", {esc(r.area)}" if r.area != "Local area" else "")
    parts.append(
        f'<div class="block hero"><div class="eyebrow">Free local visibility snapshot</div><h1>{mark(s["headline"])}</h1>'
        f'<div class="prep">Prepared for {prep}</div><div class="date">{esc(r.today)} · Prepared by Starkology</div>'
        f'<div class="pills">{pills}</div></div>')

    if s["wins"]:
        chips = "".join(f'<div class="win">{icon("check", 18)}{esc(w)}</div>' for w in s["wins"])
        parts.append(f'<div class="block sec">{head("What you already win")}<div class="chips">{chips}</div></div>')

    if s.get("score"):
        sc = s["score"]
        rows = "".join(
            f'<div class="row"><div class="l">{esc(l)}</div><div class="bar"><i style="width:{v}%;background:{STATUS[b]}"></i></div>'
            f'<div class="v">{v}%</div><div class="chip" style="background:{STATUS[b]}">{b}</div></div>' for l, v, b in s["cats"])
        parts.append(
            f'<div class="block sec">{head("Where you stand today")}<div class="card"><div class="scoretop">{_ring(sc["value"], sc["band"])}'
            f'<div><div class="verdict">{esc(sc["verdict"])}</div><div class="vline">{esc(sc["line"])}</div></div></div>'
            f'<div class="rows">{rows}</div></div></div>')

    if s["leaks"]:
        cards = []
        for i, lk in enumerate(s["leaks"], 1):
            lab = f"LEAK {i}" + (" · BIGGEST" if i == 1 else "")
            cards.append(
                f'<div class="leak"><div class="tile">{icon(lk["icon"], 28)}</div><div><div class="lab">{lab}</div>'
                f'<h3>{esc(lk["title"])}</h3><p>{esc(lk["fact"])}</p><p class="cost">What it costs you: {esc(lk["cost"])}</p></div></div>')
        k = len(s["leaks"])
        title = f"{k} leak{'s' if k != 1 else ''} costing you calls"
        parts.append(f'<div class="block sec">{head(title)}{"".join(cards)}</div>')

    if s.get("opp"):
        st = "".join(f'<div class="stat"><b>{esc(b)}</b><span>{esc(t)}</span></div>' for b, t in s["opp"]["stats"])
        parts.append(f'<div class="block sec">{head("The opportunity is open")}<div class="stats">{st}</div>'
                     f'<p class="note">{esc(s["opp"]["note"])}</p></div>')
    elif s.get("easy"):
        st = "".join(f'<div class="stat"><span style="margin:0;color:{NAVY};font-weight:700;font-size:18px">{esc(t)}</span></div>'
                     for t in s["easy"])
        parts.append(f'<div class="block sec">{head("Easy wins")}<div class="stats">{st}</div></div>')

    if s["plan"]:
        cols = "".join(
            f'<div class="col"><div class="when">{esc(c["label"])}</div><h4>{esc(c["title"])}</h4>'
            + "".join(f"<p>{esc(t)}</p>" for t in c["lines"]) + "</div>" for c in s["plan"])
        parts.append(f'<div class="block sec">{head("Your 90-day path")}<div class="plan">{cols}</div></div>')

    parts.append('<div class="cta"><div class="eyebrow">Next step · Free</div><h2>Want to discuss more? Call us</h2>'
                 '<p>A free 15-minute call. No obligation, and you keep the report either way.</p></div>')

    glows = ('<div class="glow g" style="right:-300px;top:260px"></div><div class="glow c" style="left:-330px;top:900px"></div>'
             '<div class="glow g" style="right:-320px;top:1700px"></div><div class="glow c" style="left:-320px;top:2400px"></div>')
    header = base64.b64encode(_header_png()).decode()
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{_fonts_css()}{CSS}</style></head><body>'
            f'<div id="root">{glows}<div class="content">'
            f'<img class="hdr" alt="Starkology" src="data:image/png;base64,{header}">{"".join(parts)}</div></div></body></html>')


# ------------------------------------------------------------------ rendering
def _render(html_path, out_path):
    """Runs in its own process, so it never clashes with Streamlit's event loop."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": 900}, device_scale_factor=2)
        page.goto(Path(html_path).resolve().as_uri())
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(250)
        h = page.evaluate("document.getElementById('root').scrollHeight")
        page.set_viewport_size({"width": WIDTH, "height": int(h)})
        page.screenshot(path=out_path, type="jpeg", quality=88, clip={"x": 0, "y": 0, "width": WIDTH, "height": h})
        browser.close()


def build_infographic(a, extra, others, settings, query="", rank=None, total=None):
    """The snapshot as ONE JPG (720 px layout, rendered at 2x). Returns JPEG bytes."""
    r = report._prepare(a, extra, others, settings, query, rank, total)
    html = render_html(r, _story(r))
    with tempfile.TemporaryDirectory() as tmp:
        hp, op = os.path.join(tmp, "page.html"), os.path.join(tmp, "out.jpg")
        Path(hp).write_text(html, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "render", hp, op],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not os.path.exists(op):
            raise RuntimeError("Could not draw the image (is Chromium installed? run: playwright install chromium). "
                               + (proc.stderr or "").strip()[-300:])
        return Path(op).read_bytes()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "render":
        _render(sys.argv[2], sys.argv[3])
