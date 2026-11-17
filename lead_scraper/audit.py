"""
audit.py - quick Google Maps + website SEO check for one company (homepage only).

Every check ends up as True (fine), False (problem found) or None (could not be
checked). Only checks that came back False are ever used in an email, so a
blocked or slow website never gets blamed for something we could not verify.
"""

import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REVIEW_MIN = 25      # fewer Google reviews than this counts as a problem
RATING_MIN = 4.3     # a lower Google rating counts as a problem
SERIOUS = 5          # only problems with severity >= this are used in emails
TIMEOUT = 10
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# id: (label, severity 1-9, area)
CHECKS = {
    "reviews_low": ("Google reviews", 8, "Google Maps"),
    "rating_low": ("Google rating", 7, "Google Maps"),
    "ssl_invalid": ("Valid security certificate", 9, "Website"),
    "https": ("Secure connection (HTTPS)", 8, "Website"),
    "mobile": ("Mobile-friendly setup", 8, "Website"),
    "title_local": ("Local keywords in title / heading", 7, "Website"),
    "schema": ("Local business structured data", 6, "Website"),
    "meta_desc": ("Meta description", 5, "Website"),
    "nap": ("Google phone number shown on homepage", 5, "Website"),
    "h1": ("Main heading (H1)", 4, "Website"),
    "tel_link": ("Tap-to-call link", 4, "Website"),
    "stale_year": ("Up-to-date footer year", 4, "Website"),
    "sitemap": ("XML sitemap", 3, "Website"),
    "alt_text": ("Image alt text", 3, "Website"),
    "maps_embed": ("Google Map / listing link on site", 3, "Website"),
    "heavy": ("Page weight", 3, "Website"),
}
ORDER = list(CHECKS)

LOCAL_HINTS = (
    "business", "contractor", "plumber", "electrician", "roofing", "hvac", "locksmith",
    "store", "restaurant", "dentist", "physician", "attorney", "legalservice", "salon",
    "autorepair", "autodealer", "realestateagent", "accountingservice", "financialservice",
    "movingcompany", "cleaning", "gasstation", "bakery", "cafe", "pharmacy", "gym",
)


# ----------------------------------------------------------------- HTML parsing
class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_desc = ""
        self.viewport = False
        self.h1 = []
        self.imgs = 0
        self.imgs_alt = 0
        self.scripts = 0
        self.ld = []
        self.hrefs = []
        self.iframes = []
        self.text = []
        self._cur = None
        self._buf = []
        self._skip = 0
        self._ld_script = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._cur, self._buf = "title", []
        elif tag == "h1":
            self._cur, self._buf = "h1", []
        elif tag == "script":
            self.scripts += 1
            if a.get("type", "").lower() == "application/ld+json":
                self._cur, self._buf, self._ld_script = "ld", [], True
            else:
                self._skip += 1
        elif tag == "style":
            self._skip += 1
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "description":
                self.meta_desc = a.get("content", "")
            elif name == "viewport":
                self.viewport = True
        elif tag == "img":
            self.imgs += 1
            if a.get("alt", "").strip():
                self.imgs_alt += 1
        elif tag == "a":
            if a.get("href"):
                self.hrefs.append(a["href"])
        elif tag == "iframe":
            if a.get("src"):
                self.iframes.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "title" and self._cur == "title":
            self.title = " ".join("".join(self._buf).split())
            self._cur = None
        elif tag == "h1" and self._cur == "h1":
            self.h1.append(" ".join("".join(self._buf).split()))
            self._cur = None
        elif tag == "script":
            if self._ld_script:
                self.ld.append("".join(self._buf))
                self._ld_script, self._cur = False, None
            else:
                self._skip = max(0, self._skip - 1)
        elif tag == "style":
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if self._cur in ("title", "h1", "ld"):
            self._buf.append(data)
        if self._cur == "ld":
            return
        if not self._skip:
            self.text.append(data)


def _collect_types(node, out):
    if isinstance(node, list):
        for n in node:
            _collect_types(n, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend(x for x in t if isinstance(x, str))
        for v in node.values():
            if isinstance(v, (list, dict)):
                _collect_types(v, out)


# -------------------------------------------------------------------- helpers
def first_email(value):
    return (value or "").split(",")[0].strip()


def split_emails(value):
    """'a@x.com, B@y.com; a@x.com' -> ['a@x.com', 'b@y.com'] (lower case, no duplicates, order kept)."""
    out = []
    for e in re.split(r"[,;\s]+", str(value or "")):
        e = e.strip().lower()
        if e and "@" in e and e not in out:
            out.append(e)
    return out


def company_key(row):
    """A stable id for a company. Its first email when it has one, otherwise a short hash of its Maps link
    (or name + address), so companies WITHOUT an email can still be audited, drafted and tracked."""
    e = first_email(row.get("Email")).lower()
    if "@" in e:
        return e
    basis = str(row.get("Google Maps link") or "").strip() or (
        str(row.get("Name") or "").strip().lower() + "|" + str(row.get("Address") or "").strip().lower())
    return "nm-" + hashlib.md5(basis.encode("utf-8")).hexdigest()[:10]


def _to_int(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else None


def _to_float(value):
    m = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _digits(s):
    return re.sub(r"\D", "", s)


def _fetch_home(url):
    out = {"ok": False, "ssl_bad": False, "status": None, "url": url, "text": "", "bytes": 0, "error": ""}
    for verify in (True, False):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=verify)
        except requests.exceptions.SSLError:
            if verify:
                out["ssl_bad"] = True
                continue
            out["error"] = "SSL error"
            return out
        except requests.exceptions.RequestException as e:
            out["error"] = e.__class__.__name__
            return out
        out.update(status=r.status_code, url=r.url, text=r.text[:800000], bytes=len(r.content),
                   ok=(r.status_code == 200))
        return out
    return out


def _sitemap(base, verify):
    flaky = False
    for path in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"):
        try:
            r = requests.get(base + path, headers=HEADERS, timeout=6, verify=verify)
        except requests.exceptions.RequestException:
            flaky = True
            continue
        head = r.text[:3000]
        if r.status_code == 200 and ("<urlset" in head or "<sitemapindex" in head):
            return True, f"Found at {path}"
        if r.status_code in (403, 429):
            flaky = True
    try:
        r = requests.get(base + "/robots.txt", headers=HEADERS, timeout=6, verify=verify)
        if r.status_code == 200 and "sitemap:" in r.text.lower():
            return True, "Listed in robots.txt"
    except requests.exceptions.RequestException:
        flaky = True
    if flaky:
        return None, "Could not be checked (site blocked or slow to answer)"
    return False, "No sitemap.xml found"


# ------------------------------------------------------------------ main entry
def analyze_page(page, fetched, row, city_terms, base_for_sitemap_verify=True):
    """Return {check_id: (ok, detail)} for the website checks. `page` is a parsed _Page."""
    res = {}
    final = fetched["url"]
    visible = " ".join(page.text)

    if fetched["ssl_bad"]:
        res["ssl_invalid"] = (False, "The site's security certificate is invalid or expired")
        res["https"] = (None, "See certificate check")
    else:
        res["ssl_invalid"] = (True, "Certificate accepted")
        https_ok = final.startswith("https://")
        res["https"] = (https_ok, "Site loads over HTTPS" if https_ok else f"Site loads over plain HTTP ({final})")

    res["mobile"] = (page.viewport, "Viewport tag present" if page.viewport else "No mobile viewport tag in the homepage code")

    if city_terms:
        blob = (page.title + " " + " ".join(page.h1)).lower()
        hit = [t for t in city_terms if t.lower() in blob]
        if not page.title and not any(page.h1):
            res["title_local"] = (False, "No page title or main heading found")
        elif hit:
            res["title_local"] = (True, f"Mentions {hit[0]}")
        else:
            res["title_local"] = (False, f"Title ‘{page.title[:80]}’ and headings do not mention {', '.join(city_terms)}")
    else:
        res["title_local"] = (None, "No city entered, so not checked")

    types = []
    for raw in page.ld:
        try:
            _collect_types(json.loads(raw), types)
        except Exception:
            continue
    local = [t for t in types if any(h in t.lower() for h in LOCAL_HINTS)]
    res["schema"] = (bool(local), f"Found: {local[0]}" if local else
                     ("Only generic structured data found" if types else "No structured data found"))

    desc = page.meta_desc.strip()
    res["meta_desc"] = (bool(desc), f"{len(desc)} characters" if desc else "No meta description")

    phone_digits = _digits(row.get("Phone", ""))
    if len(phone_digits) >= 7:
        tail = phone_digits[-8:] if len(phone_digits) >= 8 else phone_digits[-7:]
        shown = tail in (_digits(visible) + _digits(" ".join(page.hrefs)))
        res["nap"] = (shown, "Phone number from the Google listing is on the homepage" if shown
                      else f"Google listing phone ({row.get('Phone')}) not found on the homepage")
    else:
        res["nap"] = (None, "No phone number on the Google listing")

    h1_ok = any(h.strip() for h in page.h1)
    res["h1"] = (h1_ok, f"‘{page.h1[0][:60]}’" if h1_ok else "No H1 heading")

    tel = any(h.lower().startswith("tel:") for h in page.hrefs)
    res["tel_link"] = (tel, "Tap-to-call link present" if tel else "No tel: link on the homepage")

    m = re.search(r"(?:©|copyright)[^0-9]{0,15}((?:19|20)\d{2})(?:\s*[-–]\s*((?:19|20)\d{2}))?", visible, re.I)
    if m:
        year = int(m.group(2) or m.group(1))
        this_year = datetime.now().year
        res["stale_year"] = (year >= this_year - 2, f"Footer says © {year}")
        res["_year"] = year
    else:
        res["stale_year"] = (None, "No footer year found")

    if page.imgs >= 4:
        ratio = page.imgs_alt / page.imgs
        res["alt_text"] = (ratio >= 0.5, f"{page.imgs_alt} of {page.imgs} images have alt text")
    else:
        res["alt_text"] = (None, "Too few images to judge")

    links = page.iframes + page.hrefs
    on_map = any(k in l.lower() for l in links for k in ("google.com/maps", "goo.gl/maps", "maps.app.goo.gl", "maps.google"))
    res["maps_embed"] = (on_map, "Google Map or listing link found" if on_map else "No Google Map or listing link found")

    heavy = fetched["bytes"] > 900_000 or page.scripts > 35
    res["heavy"] = (not heavy, f"{fetched['bytes'] // 1024} KB of HTML, {page.scripts} scripts")

    # Business profiles the homepage links to (used as a partial citation signal, not a check)
    hrefs = " ".join(page.hrefs).lower()
    res["_profiles"] = [name for name, keys in PROFILE_SITES.items() if any(k in hrefs for k in keys)]
    return res


PROFILE_SITES = {
    "Facebook": ("facebook.com/",), "Instagram": ("instagram.com/",), "LinkedIn": ("linkedin.com/",),
    "Yelp": ("yelp.",), "Trustpilot": ("trustpilot.com",), "X / Twitter": ("twitter.com/", "//x.com/"),
    "YouTube": ("youtube.com/",), "TikTok": ("tiktok.com/",),
}


def gbp_from_row(row):
    """Profile facts the scraper read from the Maps page. Only values with positive evidence are kept."""
    g = {}
    for key, col in (("claimed", "Claimed"), ("hours", "Hours"), ("description", "Description")):
        v = str(row.get(col) or "").strip()
        if v in ("Yes", "No"):
            g[key] = v
    lp = str(row.get("Last post (months ago)") or "").strip()
    if lp.isdigit():
        g["last_post_months"] = int(lp)
    return g


def audit_company(row, city_terms=()):
    """Audit one scraped row (a dict with the Excel column names)."""
    website = (row.get("Website") or "").strip()
    reviews = _to_int(row.get("Reviews"))
    rating = _to_float(row.get("Rating"))
    out = {
        "company": (row.get("Name") or "").strip(),
        "email": first_email(row.get("Email")),
        "emails": split_emails(row.get("Email")),
        "key": row.get("_key") or company_key(row),
        "website": website,
        "maps": row.get("Google Maps link", ""),
        "category": row.get("Category", ""),
        "phone": row.get("Phone", ""),
        "address": row.get("Address", ""),
        "facts": {"reviews": reviews, "rating": row.get("Rating", ""), "title": "", "year": None, "profiles": []},
        "gbp": gbp_from_row(row),
        "page_status": "",
    }
    results = {}

    # --- Google Maps data we already scraped
    if reviews is None:
        results["reviews_low"] = (None, "Review count not found")
    else:
        results["reviews_low"] = (reviews >= REVIEW_MIN, f"{reviews} reviews (we look for {REVIEW_MIN}+)")
    if rating is None:
        results["rating_low"] = (None, "Rating not found")
    else:
        results["rating_low"] = (rating >= RATING_MIN, f"{rating} stars (we look for {RATING_MIN}+)")

    # --- Website
    fetched = _fetch_home(website) if website.startswith(("http://", "https://")) else None
    if fetched is None:
        out["page_status"] = "No website address"
    elif fetched["ssl_bad"] and not fetched["ok"] and not fetched["status"]:
        out["page_status"] = "Website could not be loaded"
        results["ssl_invalid"] = (False, "The site's security certificate is invalid or expired")
    elif not fetched["ok"]:
        out["page_status"] = (f"Website answered with status {fetched['status']}" if fetched["status"]
                              else f"Website could not be loaded ({fetched['error']})")
    else:
        out["page_status"] = "ok"
        page = _Page()
        try:
            page.feed(fetched["text"])
        except Exception:
            pass
        site = analyze_page(page, fetched, row, [t.strip() for t in city_terms if t.strip()])
        out["facts"]["title"] = page.title
        out["facts"]["year"] = site.pop("_year", None)
        out["facts"]["profiles"] = site.pop("_profiles", [])
        results.update(site)

        u = urlparse(fetched["url"])
        results["sitemap"] = _sitemap(f"{u.scheme}://{u.netloc}", not fetched["ssl_bad"])

    checks = []
    for cid in ORDER:
        label, sev, area = CHECKS[cid]
        ok, detail = results.get(cid, (None, out["page_status"] if area == "Website" and out["page_status"] != "ok" else "Not checked"))
        checks.append({"id": cid, "label": label, "area": area, "severity": sev, "ok": ok, "detail": detail})
    out["checks"] = checks

    known = [c for c in checks if c["ok"] is not None]
    total = sum(c["severity"] for c in known)
    failed = sum(c["severity"] for c in known if c["ok"] is False)
    # No score when the website itself could not be checked, so a blocked site never looks "perfect"
    out["score"] = round(100 * (1 - failed / total)) if total and out["page_status"] == "ok" else None

    bad = [c for c in checks if c["ok"] is False and c["severity"] >= SERIOUS]
    bad.sort(key=lambda c: (-c["severity"], ORDER.index(c["id"])))
    out["problems"] = [c["id"] for c in bad[:2]]
    return out