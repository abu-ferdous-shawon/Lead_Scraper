"""
outreach.py - drafts the email from the audit, sends it through Gmail (SMTP),
and keeps a log so the same company is never emailed twice.

The wording is rule-based (free, no AI service). Each email only states things
the audit actually measured.
"""

import csv
import json
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from xml.sax.saxutils import escape
from pathlib import Path
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOG_FIELDS = ["time", "email", "company", "subject"]


def domain_of(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _short(text, n=70):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ templates
# Each returns (what I noticed, what I would do). Short, plain sentences: long, salesy,
# link-heavy first emails are what spam filters (and people) distrust.
def _reviews_low(c):
    n = c["reviews"]
    avg = f" at {c['rating']} stars" if c.get("rating") else ""
    return (f"Your Google listing has {n} review{'' if n == 1 else 's'}{avg}, and people tend to pick the listings "
            "with more.",
            "I can set up a simple way to ask your happy customers for a review after each visit.")


def _rating_low(c):
    return (f"Your Google rating is {c['rating']} stars, which can put people off before they call.",
            "I can help you reply to the critical reviews and get recent happy customers to leave theirs.")


def _ssl_invalid(c):
    return ("Your website's security certificate looks invalid, so visitors may see a browser warning before they "
            "reach it.",
            "It's usually a quick fix: renew the certificate and send everyone to the secure version.")


def _https(c):
    return ("Your website loads without a secure connection, so Chrome can show a \"Not secure\" warning.",
            "Switching it to HTTPS is usually quick.")


def _mobile(c):
    return ("Your website doesn't seem to be set up for phones, and most local searches happen on a phone.",
            "I can make it display properly on any screen.")


def _title_local(c):
    if not c["title"]:
        return ("Your homepage doesn't seem to have a page title that tells Google what you do and where.",
                "I can write one around your main service and area.")
    return (f"Your homepage title (\"{_short(c['title'], 50)}\") doesn't mention {c['city']}, so Google has less to go "
            "on for local searches.",
            "I can rewrite the title and headings around what you do and where.")


def _schema(c):
    return ("I couldn't find local business details in your website's code, which help Google match your site to "
            "your Google listing.",
            "I can add them so they match your Google profile.")


def _meta_desc(c):
    return ("Your homepage has no description for search results, so Google writes its own.",
            "I can write one that says what you do and where.")


def _nap(c):
    return (f"The phone number on your Google listing ({c['phone']}) isn't on your website's homepage.",
            "I can make your details match across your site and your listings.")


def _h1(c):
    return ("Your homepage has no main heading, which helps Google understand what the page is about.",
            "I can add one built around your main service.")


def _tel_link(c):
    return ("Your homepage has no tap-to-call button, so people on a phone have to copy your number.",
            "I can add one.")


def _stale_year(c):
    return (f"Your website footer still says {c['year']}, which can make the site look inactive.",
            "I can refresh it.")


def _sitemap(c):
    return ("I couldn't find a sitemap on your website, which helps Google find all your pages.", "I can add one.")


def _alt_text(c):
    return ("Most images on your homepage have no alt text, so Google can't tell what they show.",
            "I can add short descriptions.")


def _maps_embed(c):
    return ("Your website doesn't show a map or link to your Google listing.", "I can add both.")


def _heavy(c):
    return ("Your homepage is quite heavy, which can make it slow to load on a phone.", "I can slim it down.")


TEMPLATES = {
    "reviews_low": _reviews_low, "rating_low": _rating_low, "ssl_invalid": _ssl_invalid, "https": _https,
    "mobile": _mobile, "title_local": _title_local, "schema": _schema, "meta_desc": _meta_desc, "nap": _nap,
    "h1": _h1, "tel_link": _tel_link, "stale_year": _stale_year, "sitemap": _sitemap, "alt_text": _alt_text,
    "maps_embed": _maps_embed, "heavy": _heavy,
}

# Words that commonly push a cold email into spam
SPAM_WORDS = ["free", "guarantee", "guaranteed", "100%", "act now", "limited time", "urgent", "click here", "buy now",
              "cheap", "discount", "special offer", "winner", "risk-free", "no obligation", "seo", "ranking",
              "rank #1", "asap", "$$$", "!!!"]
LINK_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(?:com|net|org|it|ie|io|co|uk|us|de|fr|es)\b)", re.I)


def spam_words(text):
    """Words in the text that often trigger spam filters."""
    low = (text or "").lower()
    return sorted({w for w in SPAM_WORDS if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", low)})


def has_link(text):
    return bool(LINK_RE.search(text or ""))


def _pick(name, n):
    """Stable 'random' choice per company, so emails are not word-for-word identical."""
    return sum(map(ord, name)) % n


# Optional. Empty by default: a result about one trade is wrong for every other business, so the email now
# uses only what the audit measured. Put a real, true result here only if you want it in every email.
DEFAULT_PROOF = ""
OLD_PROOF = ("{we} recently optimized another plumbing business's Google profile, helping them generate "
             "53 additional inbound calls in just 90 days.")


def clean_query(q):
    """'plumbers in cork' -> 'plumbers in Cork' (the city is shown with a capital letter)."""
    q = " ".join((q or "").replace("_", " ").split())
    if " in " in q.lower():
        head, _, tail = q.lower().rpartition(" in ")
        return f"{head} in {tail.title()}"
    return q


def city_from(query, address=""):
    """The city of the search ('plumbers in cork' -> 'Cork'), else the second-last part of the address.

    Words such as 'usa' that are not in the business address are dropped ('portland usa' -> 'Portland').
    """
    q = clean_query(query)
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if " in " in q:
        tail = q.rpartition(" in ")[2]
        low = (address or "").lower()
        kept = [w for w in tail.split() if w.lower() in low]
        return " ".join(kept) if kept else tail
    return parts[-2] if len(parts) >= 2 else ""


def _also(p, c):
    """One plain sentence about a confirmed problem."""
    if p == "rating_low":
        return (f"I also noticed your Google Business Profile has a {c['rating']}-star rating, which could be an "
                "opportunity to improve customer trust and local visibility.")
    if p == "reviews_low":
        n = c["reviews"]
        return (f"I also noticed your Google Business Profile has only {n} review{'' if n == 1 else 's'}, which could "
                "be an opportunity to improve customer trust and local visibility.")
    found = TEMPLATES[p](c)[0]
    if found.startswith(("Your ", "The ")):
        return "I also noticed that " + found[0].lower() + found[1:]
    if found.startswith("I "):
        return "Also, " + found
    return "I also noticed: " + found


def _proof(s, category):
    biz = (s.get("business") or "").strip()
    tpl = (s.get("proof") or "").strip()
    if not tpl or tpl == OLD_PROOF.strip():
        return ""
    try:
        return tpl.format(we=f"We at {biz}" if biz else "We", business=biz or "we",
                          category=(category or "local").lower())
    except (KeyError, IndexError, ValueError):
        return tpl


def _findings(a, c, comps, probs):
    """Comparison and count lines written only from numbers in the audit and the same Maps search."""
    me = (a.get("company") or "").strip().lower()
    top = [x for x in comps if x.get("rank") and x["rank"] <= 3 and (x.get("name") or "").strip().lower() != me]
    revs = [x["reviews"] for x in top if x.get("reviews") is not None]
    rats = [x["rating"] for x in top if x.get("rating") is not None]
    parts = []
    n = c.get("reviews")
    if n is not None and len(revs) >= 2:
        lo, hi = min(revs), max(revs)
        span = f"{lo:,}" if lo == hi else f"{lo:,} to {hi:,}"
        if n < lo:
            parts.append(f"For comparison, the top listings in that search have {span} reviews, against your {n:,}.")
        elif n > hi:
            parts.append(f"You already have more reviews ({n:,}) than the top listings ({span}), so what is "
                         "holding you back is elsewhere on your profile and website.")
    try:
        r = float(c.get("rating"))
    except (TypeError, ValueError):
        r = None
    if r is not None and len(rats) >= 2 and r < min(rats):
        parts.append(f"Your {r:g}-star rating is also below theirs ({min(rats):g} to {max(rats):g}).")
    if len(probs) >= 2:
        parts.append(f"In total the audit found {len(probs)} things on your Google profile and website that are "
                     "worth fixing.")
    return " ".join(parts)


def build_email(a, s, ctx=None):
    """Return (subject, body) for an audit result `a`, or None if there is nothing worth writing about.

    ctx (optional): {"query": the Maps search, "rank": this company's position, "top": names of the
    businesses that came first in the same search}. It is what lets the email say who is ahead.
    """
    ctx = ctx or {}
    probs = [p for p in (a.get("problems") or []) if p in TEMPLATES]
    rank = ctx.get("rank")
    top = [n for n in (ctx.get("top") or []) if n and n.strip().lower() != a["company"].strip().lower()][:3]
    query = clean_query(ctx.get("query", ""))
    if " in " in query:
        query = query.rpartition(" in ")[0] + " in " + city_from(query, a.get("address", ""))
    behind = bool(rank and rank > 3 and len(top) >= 2 and query)
    if not probs and not behind:
        return None

    facts = a.get("facts", {})
    name = a["company"]
    c = {"name": name, "reviews": facts.get("reviews"), "rating": facts.get("rating"), "title": facts.get("title", ""),
         "phone": a.get("phone", ""), "year": facts.get("year"),
         "city": city_from(query, a.get("address", "")) or "your area"}

    paras = []
    if behind:
        names = ", ".join(top[:-1]) + " and " + top[-1] if len(top) > 1 else top[0]
        place = "buried further down the page" if rank > 6 else "further down the list"
        paras.append(f'When someone searches Google for "{query}" right now, {names} show up in the top 3 map spots, '
                     f'while "{name}" is {place}.')
        mentioned = probs[:1]
    else:
        paras.append(f"I came across {name} on Google Maps and noticed "
                     f"{'a couple of things' if len(probs) > 1 else 'something'} worth a look.")
        mentioned = probs[:2]
    paras += [_also(p, c) for p in mentioned]
    found_line = _findings(a, c, ctx.get("comps") or [], probs)
    if found_line:
        paras.append(found_line)
    proof_line = _proof(s, a.get("category"))
    if proof_line:
        paras.append(proof_line)
    paras.append("I've prepared a brief report highlighting a few opportunities to improve your Google Business "
                 "Profile's visibility and local rankings.")
    paras.append("I'd be happy to share my findings with you. If you're interested, we can also schedule a quick call at "
                 "a time that suits you to discuss a more detailed audit and a 90-day action plan focused on getting "
                 "your business into the Google Maps Top 3.")

    me = s.get("sender_name") or "[Your name]"
    biz = s.get("business") or "[Your business]"
    lines = [f"Hi {name} Team,", ""]
    for p in paras:
        lines += [p, ""]
    lines += ["Thanks,", me, biz]
    if s.get("address"):
        lines.append(s["address"])
    subjects = [f"Question about {name}'s Google listing", f"Quick question about {name}", f"{name} on Google Maps"]
    return subjects[_pick(name, 3)], "\n".join(lines)


# -------------------------------------------------------------------- sending
def body_to_html(body, cid=None):
    """The plain-text body as simple HTML. With `cid`, the report image sits just before the 'Thanks' sign-off
    (at the end if there is no 'Thanks' paragraph)."""
    paras = [p for p in re.split(r"\n\s*\n", (body or "").strip()) if p.strip()]
    items = ['<p style="margin:0 0 14px 0">' + escape(p).replace("\n", "<br>") + "</p>" for p in paras]
    if cid:
        img = ('<p style="margin:6px 0 18px 0"><img src="cid:' + cid + '" alt="Local SEO audit report" width="700" '
               'style="display:block;width:100%;max-width:700px;height:auto;border:0"></p>')
        at = next((i for i, p in enumerate(paras) if p.strip().lower().startswith("thanks")), len(items))
        items.insert(at, img)
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#202124">'
            + "".join(items) + "</div>")


def send_email(to, subject, body, sender_name, sender_email, app_password,
               host="smtp.gmail.com", port=465, use_ssl=True, image=None, cc=None, bcc=None,
               in_reply_to=None, references=None):
    """Send the email. With `image` (JPEG bytes) the report is shown inline, under the text of the email.
    cc / bcc: comma-separated text or a list. Bcc is only an envelope recipient (never written in a header).
    in_reply_to / references: the Message-ID(s) of the email this one answers, so mail apps show it in the same
    conversation. Returns this email's own Message-ID (keep it, a later follow-up needs it)."""
    to_list, cc_list, bcc_list = split_addrs(to), split_addrs(cc), split_addrs(bcc)
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    message_id = make_msgid(domain=(sender_email.split("@")[-1] or "mail.local"))
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(x for x in [references or "", in_reply_to] if x).strip()
    msg.set_content(body)
    if image:
        cid = make_msgid(domain=(sender_email.split("@")[-1] or "report.local"))
        msg.add_alternative(body_to_html(body, cid[1:-1]), subtype="html")
        msg.get_payload()[1].add_related(image, "image", "jpeg", cid=cid, filename="audit-report.jpg",
                                         disposition="inline")
    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
    with server:
        if app_password:
            server.login(sender_email, app_password.replace(" ", ""))
        everyone = []
        for a_ in to_list + cc_list + bcc_list:
            if a_.lower() not in [x.lower() for x in everyone]:
                everyone.append(a_)
        server.send_message(msg, to_addrs=everyone)
    return message_id


def split_addrs(value):
    """'a@x.com, b@y.com; c@z.com' (or a list) -> ['a@x.com', 'b@y.com', 'c@z.com']"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        value = ",".join(value)
    return [p.strip() for p in re.split(r"[,;\s]+", str(value)) if p.strip()]


def bad_addrs(value):
    """The addresses in `value` that do not look like email addresses."""
    return [a_ for a_ in split_addrs(value) if not EMAIL_RE.match(a_)]


# ------------------------------------------------------------ sent copies
def save_sent_copy(folder, search, company, to, cc, bcc, subject, body, image=None, message_id="",
                   references="", kind="first"):
    """Keep what was sent (text, Cc/Bcc, report image, time) so the Email tracker page can show it later.
    `search` is the name of the results file the company came from (e.g. 'dentist_in_dhaka_bangladesh')."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    base = (now.strftime("%Y%m%d_%H%M%S_") + re.sub(r"[^a-z0-9]+", "_", (to or "").lower()).strip("_"))[:90]
    if image:
        (folder / (base + ".jpg")).write_bytes(image)
    rec = {"time": now.strftime("%Y-%m-%d %H:%M:%S"), "search": search, "company": company, "to": to,
           "cc": cc or "", "bcc": bcc or "", "subject": subject, "body": body,
           "image": (base + ".jpg") if image else "", "file": base + ".json",
           "message_id": message_id or "", "references": references or "", "kind": kind}
    (folder / (base + ".json")).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def read_sent_copies(folder):
    """All saved copies, newest first."""
    out = []
    for f in Path(folder).glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            rec.setdefault("file", f.name)
            out.append(rec)
        except Exception:
            continue
    return sorted(out, key=lambda r: r.get("time", ""), reverse=True)


# ------------------------------------------------------------------- sent log
def read_log(path):
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def sent_emails(path):
    return {r["email"].lower() for r in read_log(path)}


def sent_today(path):
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for r in read_log(path) if r["time"].startswith(today))


def log_sent(path, email, company, subject):
    path = Path(path)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "email": email.lower(),
                    "company": company, "subject": subject})


# ------------------------------------------------------------------- settings
def load_settings(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")