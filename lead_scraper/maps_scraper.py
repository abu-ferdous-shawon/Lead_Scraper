"""
maps_scraper.py  -  no API key needed.
Searches Google Maps in a real browser window and saves the companies to Excel.

Usage:
    python maps_scraper.py "roofing companies in Rome"
    python maps_scraper.py "plumbers in Milan" --max 40
    python maps_scraper.py            (it will ask you for the search)

Setup (once):
    pip install playwright pandas openpyxl requests
    playwright install chromium

Notes:
  * A browser window opens on purpose. If Google shows a CAPTCHA or a consent
    page, solve/accept it in the window and the script carries on.
  * Google changes its page layout from time to time. If fields come back
    empty, the selectors in extract_details() need updating.
  * Scraping Google Maps goes against Google's Terms of Service. Keep volumes
    small and the pace slow.
"""

import argparse
import html
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from urllib.parse import quote

import pandas as pd
import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright


def pause(a=0.8, b=1.8):
    time.sleep(random.uniform(a, b))


def accept_consent(page):
    """Click through Google's cookie/consent page if it shows up."""
    for label in ["Accept all", "I agree", "Reject all"]:
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=3000)
                pause()
                return
        except Exception:
            pass


def collect_links(page, max_results):
    """Scroll the results list and return unique place links."""
    try:
        page.wait_for_selector('div[role="feed"]', timeout=20000)
    except PWTimeout:
        print("Results list did not load. If there is a CAPTCHA, solve it in the window, then rerun.")
        return []

    feed = page.locator('div[role="feed"]')
    links, stale_rounds = [], 0

    while len(links) < max_results and stale_rounds < 4:
        hrefs = page.eval_on_selector_all(
            'div[role="feed"] a[href*="/maps/place/"]',
            "els => els.map(e => e.href)",
        )
        before = len(links)
        for h in hrefs:
            if h not in links:
                links.append(h)
        stale_rounds = stale_rounds + 1 if len(links) == before else 0

        if page.get_by_text("You've reached the end of the list").count():
            break

        feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        pause(1.2, 2.2)

    return links[:max_results]


def text_of(page, selector):
    try:
        loc = page.locator(selector).first
        if loc.count():
            return loc.inner_text(timeout=2000).strip()
    except Exception:
        pass
    return ""


def attr_of(page, selector, attr):
    try:
        loc = page.locator(selector).first
        if loc.count():
            return (loc.get_attribute(attr, timeout=2000) or "").strip()
    except Exception:
        pass
    return ""


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
JUNK = ("example.", "sentry", "wixpress", "domain.com", "email.com", "yourname", "yourdomain", "@2x")
BAD_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
CONTACT_PATHS = ["", "/contact", "/contacts", "/contact-us", "/contact-us/", "/contact.html", "/contact-us.html",
                 "/get-in-touch", "/about", "/about-us", "/contatti", "/contattaci", "/chi-siamo", "/kontakt",
                 "/impressum", "/privacy-policy", "/privacy", "/terms", "/terms-and-conditions", "/team"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def find_emails(website):
    """Look for email addresses on a company's homepage and common contact pages."""
    m = re.match(r"(https?://[^/]+)", website or "")
    if not m:
        return ""
    root = m.group(1)
    domain = root.split("//")[1].replace("www.", "").lower()
    found = []

    def get(url):
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
        except requests.exceptions.SSLError:
            try:  # sites with a broken certificate: still just reading public text
                r = requests.get(url, headers=HEADERS, timeout=8, verify=False)
            except Exception:
                return None
        except Exception:
            return None
        return r if r.status_code == 200 else None

    def harvest(raw):
        text = html.unescape(raw).replace("%40", "@")
        # Cloudflare "email protection": data-cfemail="hex"
        for h in re.findall(r"""data-cfemail=["']([0-9a-fA-F]+)["']""", text):
            try:
                k = int(h[:2], 16)
                text += " " + "".join(chr(int(h[i:i + 2], 16) ^ k) for i in range(2, len(h), 2))
            except ValueError:
                pass
        text = re.sub(r"\s*[\[\(\{]\s*(?:at|AT)\s*[\]\)\}]\s*", "@", text)
        text = re.sub(r"\s*[\[\(\{]\s*(?:dot|DOT)\s*[\]\)\}]\s*", ".", text)
        for e in EMAIL_RE.findall(text):
            e = e.strip(".").lower()
            if e.endswith(BAD_EXT) or any(j in e for j in JUNK):
                continue
            if e not in found:
                found.append(e)

    # homepage first, then the contact-like pages it links to, then the usual paths
    pages, seen = [], set()
    home = get(root)
    if home is not None:
        harvest(home.text)
        for href in re.findall(r'href=["\']([^"\'#]+)["\']', home.text, flags=re.I):
            if re.search(r"contact|about|team|staff|impressum|kontakt|contatti|chi-siamo|reach|connect", href, re.I):
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = root + href
                elif not href.startswith("http"):
                    href = root + "/" + href
                if href.startswith(root) and href not in seen and not href.lower().startswith("mailto"):
                    seen.add(href)
                    pages.append(href)
    for path in CONTACT_PATHS:
        if path and root + path not in seen:
            pages.append(root + path)
    if not found:  # still nothing: the sitemap lists pages the homepage menu does not link to
        sm = get(root + "/sitemap.xml")
        if sm is not None:
            for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm.text):
                if re.search(r"contact|about|team|staff|impressum|kontakt|contatti|reach|privacy|terms", u, re.I) \
                        and u not in seen and u.startswith(root) and not u.endswith(".xml"):
                    seen.add(u)
                    pages.insert(0, u)
    if not found:
        for url in pages[:10]:
            r = get(url)
            if r is not None:
                harvest(r.text)
            if found:
                break
            time.sleep(0.4)

    found.sort(key=lambda e: domain not in e)  # emails on the company's own domain first
    return ", ".join(found[:5])


AGO_RE = re.compile(r"\b(\d+|a|an|one)\s+(day|week|month|year)s?\s+ago\b", re.I)


def months_ago(text):
    """'3 weeks ago' -> 1, '9 months ago' -> 9, 'a year ago' -> 12. None if no date is found."""
    m = AGO_RE.search(text or "")
    if not m:
        return None
    n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
    unit = m.group(2).lower()
    if unit == "day":
        return 0
    if unit == "week":
        return n // 4
    return n if unit == "month" else n * 12


def read_profile(main_text, about_text, updates_text, tabs, has_hours_button):
    """Turn the text of the Maps panel into profile facts. Pure function (easy to test).

    A value is only filled when the page gives positive evidence. Anything unclear stays empty
    and the report marks it Unverified, so a layout change can never invent a problem.
    """
    out = {"Claimed": "", "Hours": "", "Description": "", "Last post (months ago)": ""}
    panel_loaded = bool(main_text.strip())
    if panel_loaded:
        out["Claimed"] = "No" if re.search(r"claim this business", main_text, re.I) else "Yes"
        if has_hours_button or re.search(r"\b(open 24 hours|opens?\s+\d|closes?\s+\d|open\s*[⋅·]|closed\s*[⋅·])", main_text, re.I):
            out["Hours"] = "Yes"
        elif re.search(r"add (business )?hours", main_text, re.I):
            out["Hours"] = "No"
    if about_text:
        if re.search(r"from the business", about_text, re.I):
            out["Description"] = "Yes"
        elif len(about_text.strip()) > 40:
            out["Description"] = "No"
    if updates_text:
        m = months_ago(updates_text)
        if m is not None:
            out["Last post (months ago)"] = str(m)
    return out


def _click_tab(page, pattern):
    try:
        tab = page.get_by_role("tab", name=re.compile(pattern, re.I))
        if tab.count():
            tab.first.click(timeout=3000)
            pause(0.8, 1.4)
            return True
    except Exception:
        pass
    return False


def extract_profile(page):
    """Hours, claimed status, description and last post, from the panel that is already open."""
    main_sel = 'div[role="main"]'
    main_text = text_of(page, main_sel)
    try:
        tabs = [t.strip() for t in page.locator('button[role="tab"]').all_inner_texts()]
    except Exception:
        tabs = []
    has_hours = False
    try:
        has_hours = page.locator('[data-item-id="oh"], [aria-label*="hours" i]').count() > 0
    except Exception:
        pass
    about = updates = ""
    if _click_tab(page, r"^about"):
        about = text_of(page, main_sel)
    if _click_tab(page, r"^updates?"):
        updates = text_of(page, main_sel)
    return read_profile(main_text, about, updates, tabs, has_hours)


def extract_details(page, url):
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("h1", timeout=10000)
    except PWTimeout:
        return None
    pause(0.8, 1.4)

    name = text_of(page, "h1")
    address = text_of(page, 'button[data-item-id="address"]')
    phone = text_of(page, 'button[data-item-id^="phone"]')
    website = attr_of(page, 'a[data-item-id="authority"]', "href")
    category = text_of(page, "button.DkEaL")
    rating = text_of(page, 'div.F7nice span[aria-hidden="true"]')

    reviews = ""
    label = attr_of(page, 'div.F7nice span[aria-label*="review"]', "aria-label")
    m = re.search(r"([\d,\.]+)", label)
    if m:
        reviews = m.group(1)

    # Clean the little icon characters Google puts in front of some values
    clean = lambda s: re.sub(r"^[^\w+(]+", "", s).strip()
    try:
        profile = extract_profile(page)
    except Exception:
        profile = {"Claimed": "", "Hours": "", "Description": "", "Last post (months ago)": ""}
    return {
        "Name": name,
        "Category": category,
        "Address": clean(address),
        "Phone": clean(phone),
        "Website": website,
        "Email": "",
        "Rating": rating,
        "Reviews": reviews,
        "Google Maps link": url,
        **profile,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", help='e.g. "roofing companies in Rome"')
    parser.add_argument("--max", type=int, default=30, help="max companies (default 30)")
    parser.add_argument("--headless", action="store_true", help="hide the browser window")
    parser.add_argument("--no-email", action="store_true", help="skip the email search (faster)")
    parser.add_argument("--out", default=".", help="folder to save the results in")
    args = parser.parse_args()

    query = args.query or input("What do you want to search? ").strip()
    if not query:
        sys.exit("Empty search.")

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(locale="en-US", viewport={"width": 1300, "height": 900})
        page = context.new_page()

        print(f"Searching: {query}")
        page.goto(f"https://www.google.com/maps/search/{quote(query)}?hl=en", wait_until="domcontentloaded")
        pause(1.5, 2.5)
        accept_consent(page)

        links = collect_links(page, args.max)
        print(f"Found {len(links)} listings, reading details...")

        detail = context.new_page()
        for i, url in enumerate(links, 1):
            try:
                info = extract_details(detail, url)
            except Exception as e:
                print(f"  {i}/{len(links)} skipped ({e.__class__.__name__})")
                continue
            if info and info["Name"]:
                if not args.no_email:
                    info["Email"] = find_emails(info["Website"])
                rows.append(info)
                print(f"  {i}/{len(links)} {info['Name']}  {info['Email']}")
            pause(1.0, 2.0)

        browser.close()

    if not rows:
        sys.exit("No data collected.")

    df = pd.DataFrame(rows).drop_duplicates(subset=["Name", "Address"])
    if args.no_email:
        df = df.drop(columns=["Email"])
    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, re.sub(r"[^A-Za-z0-9]+", "_", query).strip("_").lower())

    # Backup first, so the data survives even if the Excel save fails
    csv_name = f"{base}.csv"
    try:
        df.to_csv(csv_name, index=False, encoding="utf-8-sig")
    except PermissionError:
        csv_name = f"{base}_{time.strftime('%H%M%S')}.csv"
        df.to_csv(csv_name, index=False, encoding="utf-8-sig")

    filename = f"{base}.xlsx"
    try:
        df.to_excel(filename, index=False)
    except PermissionError:
        # Usually the file is open in Excel. Save under a new name instead of crashing.
        filename = f"{base}_{time.strftime('%H%M%S')}.xlsx"
        df.to_excel(filename, index=False)
        print("The old Excel file is open in another program, so I saved a new one.")
    # Remember what was searched (the report and the email quote it) and when
    try:
        Path(filename).with_suffix(".meta.json").write_text(
            json.dumps({"query": query, "date": time.strftime("%Y-%m-%d")}), encoding="utf-8")
    except Exception:
        pass
    print(f"Saved {len(df)} companies to {filename} (backup: {csv_name})")
    print(f"RESULT_FILE={filename}")


if __name__ == "__main__":
    main()