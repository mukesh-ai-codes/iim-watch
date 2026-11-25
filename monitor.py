"""theIPMlabs official-update watcher.

Checks every page in urls.txt, finds NEW links/notices since the last run,
and alerts via Telegram / Email / WhatsApp (whichever secrets are set).
State is kept in state.json (committed back to the repo by the workflow).
"""
import hashlib
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText

import requests
import urllib3
from bs4 import BeautifulSoup

try:  # browser-like fetcher: gets past many "403 Forbidden" bot blocks
    from curl_cffi import requests as cffi_requests
except Exception:  # noqa: BLE001
    cffi_requests = None

urllib3.disable_warnings()

BASE = os.path.dirname(os.path.abspath(__file__))
URLS_FILE = os.path.join(BASE, "urls.txt")
STATE_FILE = os.path.join(BASE, "state.json")

FAIL_ALERT_AFTER = 4        # consecutive failed checks before a "site down" alert
BIG_CHANGE = 30             # more new links than this = probably a redesign
MAX_LINKS_PER_SITE = 12     # cap links listed per site in one alert
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-IN,en;q=0.9",
}
SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "whatsapp:", "#")


# ---------------------------------------------------------------- helpers
def load_urls():
    sites = []
    with open(URLS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            name, url = parts[0], parts[1]
            # optional 3rd field "text": also alert when the page's wording/dates change
            mode = parts[2].lower() if len(parts) > 2 else ""
            sites.append((name, url, mode))
    return sites


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False, sort_keys=True)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


OFFICIAL_SUFFIXES = (".ac.in", ".edu.in", ".nic.in", ".gov.in", ".edu")


def site_key(host):
    """Registered domain, e.g. www.iimidr.ac.in -> iimidr.ac.in, exams.nta.nic.in -> nta.nic.in."""
    parts = host.lower().split(":")[0].split(".")
    if len(parts) >= 3 and parts[-2] in ("ac", "edu", "nic", "gov", "co", "org", "res"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_relevant(page_url, link_url):
    """Keep only links on the same website (blocks injected spam / ads / social links).
    Also keep PDFs/docs hosted on other official Indian education/government sites."""
    a = urllib.parse.urlsplit(page_url).netloc
    b = urllib.parse.urlsplit(link_url).netloc
    if site_key(a) == site_key(b):
        return True
    path = urllib.parse.urlsplit(link_url).path.lower()
    return path.endswith((".pdf", ".doc", ".docx")) and b.lower().endswith(OFFICIAL_SUFFIXES)


def norm_url(base, href):
    href = href.strip()
    if not href or href.lower().startswith(SKIP_SCHEMES):
        return None
    full = urllib.parse.urljoin(base, href)
    p = urllib.parse.urlsplit(full)
    if p.scheme not in ("http", "https"):
        return None
    # drop fragments and common cache-buster / session params
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if k.lower() not in ("v", "ver", "_", "t", "ts", "sid", "phpsessid", "utm_source",
                              "utm_medium", "utm_campaign")]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path, urllib.parse.urlencode(q), ""))


def fetch(url):
    """Try a real-browser fingerprint first, then plain requests."""
    errors = []
    tries = []
    if cffi_requests is not None:
        tries.append(lambda: cffi_requests.get(url, impersonate="chrome", timeout=45,
                                               verify=False, allow_redirects=True))
    tries.append(lambda: requests.get(url, headers=HEADERS, timeout=45, verify=False))
    for attempt in range(2):
        for get in tries:
            try:
                r = get()
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        time.sleep(3)
    first = errors[0]
    raise RuntimeError(str(first)[:150] + (" [browser mode]" if cffi_requests else " [basic mode]"))


# lines that change on their own (site-wide stamps) — never alert on these
NOISE_LINE = re.compile(r"last\s*updated|visitor|hit\s*count|copyright|©|all rights reserved", re.I)


def snapshot(url):
    """Return (links:dict url->text, text_hash, text_lines) for a page."""
    r = fetch(url)
    ctype = r.headers.get("Content-Type", "")
    if "html" not in ctype and not r.text.lstrip().startswith("<"):
        return {}, hashlib.sha256(r.content).hexdigest(), []
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    links = {}
    for a in soup.find_all("a", href=True):
        u = norm_url(r.url, a["href"])
        if not u or not is_relevant(url, u):
            continue
        text = clean(a.get_text(" ")) or clean(a.get("title")) or u.rsplit("/", 1)[-1]
        if u not in links or len(text) > len(links[u]):
            links[u] = text[:200]
    # visible text, line by line (for "text" mode pages such as key-dates tables)
    lines, seen = [], set()
    for ln in soup.get_text("\n").split("\n"):
        ln = clean(ln)[:300]
        if ln and ln not in seen and not NOISE_LINE.search(ln):
            seen.add(ln)
            lines.append(ln)
    # visible-text hash (numbers stripped so counters/clocks don't trigger)
    body = clean(soup.get_text(" "))
    body = re.sub(r"\d+", "", body)
    return links, hashlib.sha256(body.encode()).hexdigest(), lines


def check(site):
    name, url, mode = site
    try:
        links, h, lines = snapshot(url)
        return name, url, mode, links, h, lines, None
    except Exception as e:  # noqa: BLE001
        return name, url, mode, None, None, None, f"{type(e).__name__}: {str(e)[:120]}"


# ---------------------------------------------------------------- notify
def send_telegram(msg):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    for i in range(0, len(msg), 3900):
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": msg[i:i + 3900],
                                "disable_web_page_preview": "true"}, timeout=30)
        except Exception as e:  # noqa: BLE001
            print("Telegram error:", e)


def send_email(subject, msg):
    user, pwd = os.getenv("EMAIL_USER"), os.getenv("EMAIL_APP_PASSWORD")
    to = os.getenv("EMAIL_TO") or user
    if not user or not pwd:
        return
    try:
        m = MIMEText(msg, "plain", "utf-8")
        m["Subject"], m["From"], m["To"] = subject, user, to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, [t.strip() for t in to.split(",")], m.as_string())
    except Exception as e:  # noqa: BLE001
        print("Email error:", e)


def send_whatsapp(msg):
    phone, key = os.getenv("WA_PHONE"), os.getenv("WA_APIKEY")
    if not phone or not key:
        return
    for i in range(0, len(msg), 1500):
        try:
            requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": phone, "text": msg[i:i + 1500], "apikey": key},
                         timeout=40)
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            print("WhatsApp error:", e)


def notify(subject, msg):
    print("=" * 60, "\n" + msg)
    send_telegram(msg)
    send_email(subject, msg)
    send_whatsapp(msg)


# ---------------------------------------------------------------- main
def main():
    sites = load_urls()
    state = load_state()
    first_run = not state
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(check, sites))

    updates, down, recovered, ok, failed = [], [], [], 0, []
    for name, url, mode, links, h, lines, err in results:
        s = state.setdefault(url, {"name": name, "links": {}, "hash": None, "fails": 0})
        s["name"] = name
        if err:
            s["fails"] = s.get("fails", 0) + 1
            failed.append(f"• {name} — {err}")
            if s["fails"] == FAIL_ALERT_AFTER:
                down.append(f"• {name}\n  {url}\n  ({err})")
            continue
        ok += 1
        if s.get("fails", 0) >= FAIL_ALERT_AFTER:
            recovered.append(f"• {name}")
        s["fails"] = 0
        old_links = s.get("links", {})
        new = {u: t for u, t in links.items() if u not in old_links}
        # same link, new wording (e.g. "BMS AT 2025" -> "BMS AT 2027")
        renamed = {u: (old_links[u], t) for u, t in links.items()
                   if u in old_links and t and old_links[u]
                   and t.lower() != old_links[u].lower()}
        if s.get("hash") is not None and not first_run:
            if len(new) + len(renamed) > BIG_CHANGE:
                updates.append(f"🔄 {name}: page changed a lot ({len(new) + len(renamed)} "
                               f"links new/changed — possibly a redesign). Check: {url}")
            elif new or renamed:
                lines = [f"📢 {name}"]
                for u, t in list(new.items())[:MAX_LINKS_PER_SITE]:
                    lines.append(f"  • {t}\n    {u}")
                if len(new) > MAX_LINKS_PER_SITE:
                    lines.append(f"  …and {len(new) - MAX_LINKS_PER_SITE} more: {url}")
                for u, (old_t, t) in list(renamed.items())[:MAX_LINKS_PER_SITE]:
                    lines.append(f"  ✏️ {t}\n    (was: {old_t})\n    {u}")
                updates.append("\n".join(lines))
            elif h != s.get("hash") and not links:
                updates.append(f"📄 {name}: file/page content changed\n  {url}")
        if mode == "text":
            old_lines = s.get("text")
            if old_lines is not None and not first_run:
                old_set, new_set = set(old_lines), set(lines)
                added = [ln for ln in lines if ln not in old_set]
                removed = [ln for ln in old_lines if ln not in new_set]
                if added or removed:
                    msg = [f"🗓️ {name}: page text changed\n  {url}"]
                    msg += [f"  ➕ {ln}" for ln in added[:8]]
                    msg += [f"  ➖ {ln}" for ln in removed[:8]]
                    if len(added) > 8 or len(removed) > 8:
                        msg.append("  …more changes — open the page")
                    updates.append("\n".join(msg))
            s["text"] = lines
        else:
            s.pop("text", None)
        # keep union of links so items that flicker don't re-alert
        merged = dict(old_links)
        merged.update(links)
        s["links"] = merged
        s["hash"] = h

    # forget sites removed from urls.txt
    current = {u for _, u, _ in sites}
    for u in list(state):
        if u not in current:
            del state[u]
    save_state(state)

    if first_run:
        msg = (f"✅ theIPMlabs watcher is LIVE\n{ok}/{len(sites)} pages reachable.\n"
               "You'll get a message whenever an official page adds something new.")
        if failed:
            msg += "\n\nCould not reach (will keep retrying):\n" + "\n".join(failed)
        notify("theIPMlabs watcher is live", msg)
        return

    parts = []
    if updates:
        parts.append("🚨 NEW OFFICIAL UPDATES\n\n" + "\n\n".join(updates))
    if down:
        parts.append("⚠️ Not reachable for ~1 hour:\n" + "\n".join(down))
    if recovered:
        parts.append("✅ Back online:\n" + "\n".join(recovered))
    if parts:
        notify("IPM/IIM official update", "\n\n".join(parts) +
               "\n\n(Always open the official link before posting.)")
    else:
        print(f"No changes. {ok}/{len(sites)} reachable.")
        if failed:
            print("\n".join(failed))


if __name__ == "__main__":
    sys.exit(main())
