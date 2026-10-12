#!/usr/bin/env python3
"""Weekly Crossref pull for the Threshold Research Group 'Evidence & Policy Watch'.

Pulls recent journal-article records for the 15 tracked journals from the Crossref REST API
(polite pool) and writes them to a static folder that GitHub Pages serves, so the weekly
issue-building run makes one fetch of data/index.json instead of 15-20 calls to
api.crossref.org through a rate-limited proxy.

Outputs (under --out, default ./data):
  index.json            one entry per journal, in the fixed ISSN order, with compact item records
  index.md              the same listing as readable markdown (for checking by eye)
  abstracts/<slug>.json one file per article carrying the full abstract (JATS tags stripped)

Standard library only; no pip installs. Python 3.9+.

Usage:
  python fetch_crossref.py --out data --mailto info@example.org --days 35 [--base-url https://user.github.io/repo]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.crossref.org/v1/works"

# Fixed order: this is the order the issue's "journals" array must follow.
JOURNALS = [
    ("Criminology", "0011-1384", "published"),
    ("Criminology & Public Policy", "1745-9133", "published"),
    ("Journal of Criminal Justice", "0047-2352", "created"),   # Elsevier: 'published' is the issue date
    ("Journal of Experimental Criminology", "1573-3750", "published"),
    ("Journal of Quantitative Criminology", "0748-4518", "published"),
    ("Journal of Developmental and Life-Course Criminology", "2199-4641", "published"),
    ("Justice Quarterly", "0741-8825", "published"),
    ("Crime & Delinquency", "0011-1287", "published"),
    ("British Journal of Criminology", "0007-0955", "published"),
    ("Australian & New Zealand Journal of Criminology", "0004-8658", "published"),
    ("Criminal Justice and Behavior", "0093-8548", "published"),
    ("Sexual Abuse", "1079-0632", "published"),
    ("Journal of Sexual Aggression", "1355-2600", "published"),
    ("Trauma, Violence, & Abuse", "1524-8380", "published"),
    ("Campbell Systematic Reviews", "1891-1803", "published"),
]

# Titles starting with these are dropped (and listed under 'dropped' so nothing vanishes silently).
DROP_PREFIXES = ("correction", "erratum", "corrigendum", "retraction", "editorial", "book review")
# Campbell Systematic Reviews: protocols are dropped too.
PROTOCOL_ISSN = "1891-1803"

SELECT = "DOI,title,published,published-online,created,container-title,author,abstract,license,URL,type"
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get_json(url: str, mailto: str, tries: int = 6) -> dict:
    """GET with polite-pool headers; honours Retry-After on 429/503; exponential back-off otherwise."""
    headers = {
        "User-Agent": f"EvidencePolicyWatch/1.0 (https://thresholdresearchgroup.com.au; mailto:{mailto})",
        "Accept": "application/json",
    }
    delay = 5
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < tries:
                wait = e.headers.get("Retry-After")
                wait_s = int(wait) if wait and wait.isdigit() else delay
                log(f"  HTTP {e.code}; waiting {wait_s}s (attempt {attempt}/{tries})")
                time.sleep(wait_s)
                delay = min(delay * 2, 120)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries:
                log(f"  network error {e}; waiting {delay}s (attempt {attempt}/{tries})")
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise
    raise RuntimeError("unreachable")


def date_parts(obj: dict | None) -> str | None:
    """Crossref date object -> 'YYYY-MM-DD' (or 'YYYY-MM' / 'YYYY' when that is all there is)."""
    if not obj:
        return None
    parts = (obj.get("date-parts") or [[]])[0]
    if not parts:
        return None
    parts = [int(p) for p in parts if p is not None]
    if len(parts) >= 3:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    if len(parts) == 2:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    return f"{parts[0]:04d}"


def date_time(obj: dict | None) -> str | None:
    """Crossref 'created' object -> 'YYYY-MM-DD' from its date-time field."""
    if not obj:
        return None
    s = obj.get("date-time")
    if s:
        return s[:10]
    return date_parts(obj)


def clean_abstract(raw: str | None) -> str | None:
    if not raw:
        return None
    text = TAG_RE.sub(" ", raw)          # strip JATS/XML tags
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    # Drop a leading 'Abstract' label that some publishers embed.
    text = re.sub(r"^(abstract|summary)[\s:.-]*", "", text, flags=re.I)
    return text or None


def author_names(authors: list | None) -> list[str]:
    out = []
    for a in authors or []:
        fam = a.get("family")
        if fam:
            out.append(fam.strip())
        elif a.get("name"):
            out.append(a["name"].strip())
    return out


def author_label(names: list[str]) -> str:
    if not names:
        return "Unattributed"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def is_open_access(licenses: list | None) -> bool:
    for lic in licenses or []:
        url = (lic.get("URL") or "").lower()
        if "creativecommons.org" in url:
            return True
    return False


def drop_reason(title: str, issn: str) -> str | None:
    t = title.strip().lower()
    for p in DROP_PREFIXES:
        if t.startswith(p):
            return p
    if issn == PROTOCOL_ISSN and (t.startswith("protocol") or "protocol:" in t[:40]):
        return "protocol"
    return None


def slug_for(doi: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", doi.lower()).strip("-")


def display_date(iso: str | None) -> str | None:
    """'2026-10-05' -> '5 Oct 2026' (the form used in the issue's journal lists)."""
    if not iso or len(iso) < 10:
        return iso
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%b %Y')}"


def long_date(iso: str | None) -> str | None:
    """'2026-10-05' -> '5 October 2026' (the form used in pick/contrast meta lines)."""
    if not iso or len(iso) < 10:
        return iso
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B %Y')}"


def build_item(rec: dict, journal: str, issn: str, sort_key: str, base_url: str, out_dir: Path) -> dict:
    doi = rec.get("DOI", "")
    title = " ".join((rec.get("title") or [""])[0].split())
    names = author_names(rec.get("author"))
    published = date_parts(rec.get("published-online")) or date_parts(rec.get("published"))
    created = date_time(rec.get("created"))
    online = created if sort_key == "created" else (published or created)
    abstract = clean_abstract(rec.get("abstract"))
    slug = slug_for(doi)
    abstract_rel = f"abstracts/{slug}.json"
    item = {
        "doi": doi,
        "url": f"https://doi.org/{doi}",
        "title": title,
        "journal": journal,
        "issn": issn,
        "authors": names,
        "author_label": author_label(names),
        "published": published,
        "created": created,
        "online_date": online,
        "date_label": display_date(online) + (" (in press)" if sort_key == "created" else ""),
        "meta_date": long_date(online),
        "open_access": is_open_access(rec.get("license")),
        "has_abstract": abstract is not None,
        "abstract_url": f"{base_url}/{abstract_rel}" if base_url else abstract_rel,
    }
    (out_dir / "abstracts").mkdir(parents=True, exist_ok=True)
    with open(out_dir / abstract_rel, "w", encoding="utf-8") as f:
        json.dump({**item, "abstract": abstract, "publisher_url": rec.get("URL")}, f, ensure_ascii=False, indent=1)
    return item


def fetch_journal(name: str, issn: str, sort_key: str, since: str, mailto: str, rows: int) -> dict:
    date_filter = "from-created-date" if sort_key == "created" else "from-pub-date"
    params = {
        "filter": f"issn:{issn},{date_filter}:{since},type:journal-article",
        "sort": sort_key,
        "order": "desc",
        "rows": str(rows),
        "select": SELECT,
        "mailto": mailto,
    }
    url = API + "?" + urllib.parse.urlencode(params, safe=":,-")
    log(f"{name} ({issn}) sort={sort_key}")
    data = get_json(url, mailto)
    msg = data.get("message", {})
    return {"items": msg.get("items", []), "total": msg.get("total-results", 0), "query": url}


def write_markdown(index: dict, path: Path) -> None:
    lines = [f"# Crossref pull — generated {index['generated_at_brisbane']} (Brisbane)",
             "", f"Window: articles dated on or after {index['window_from']} ({index['window_days']} days). "
             "Items in the issue's 21-day window are a subset; apply the window at build time using `online_date`.", ""]
    for j in index["journals"]:
        status = f"{len(j['items'])} item(s)" if j["ok"] else f"FAILED: {j['error']}"
        lines.append(f"## {j['name']} — {status}")
        for it in j["items"]:
            oa = " · open access" if it["open_access"] else ""
            ab = "" if it["has_abstract"] else " · no abstract"
            lines.append(f"- {it['title']} — {it['author_label']} · {it['date_label']}{oa}{ab} · https://doi.org/{it['doi']}")
        for d in j["dropped"]:
            lines.append(f"- (dropped: {d['reason']}) {d['title']} · https://doi.org/{d['doi']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--mailto", required=True)
    ap.add_argument("--days", type=int, default=35, help="look-back window in days (wider than the issue's 21)")
    ap.add_argument("--rows", type=int, default=100)
    ap.add_argument("--base-url", default="", help="public URL of the out folder, e.g. https://user.github.io/repo/data")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between Crossref requests")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")

    now_utc = dt.datetime.now(dt.timezone.utc)
    brisbane = now_utc.astimezone(dt.timezone(dt.timedelta(hours=10)))
    since = (now_utc.date() - dt.timedelta(days=args.days)).isoformat()

    journals_out = []
    failures = 0
    for name, issn, sort_key in JOURNALS:
        entry = {"name": name, "issn": issn, "sort": sort_key, "ok": False, "error": None,
                 "total_results": 0, "items": [], "dropped": []}
        try:
            res = fetch_journal(name, issn, sort_key, since, args.mailto, args.rows)
            entry["ok"] = True
            entry["total_results"] = res["total"]
            for rec in res["items"]:
                title = " ".join((rec.get("title") or [""])[0].split())
                reason = drop_reason(title, issn)
                if reason:
                    entry["dropped"].append({"doi": rec.get("DOI", ""), "title": title, "reason": reason})
                    continue
                entry["items"].append(build_item(rec, name, issn, sort_key, base_url, out_dir))
        except Exception as e:  # keep going: one journal failing must not sink the file
            failures += 1
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"  FAILED: {entry['error']}")
        journals_out.append(entry)
        time.sleep(args.pause)

    index = {
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_brisbane": brisbane.strftime("%Y-%m-%d %H:%M AEST"),
        "window_from": since,
        "window_days": args.days,
        "journal_count": len(journals_out),
        "journals_failed": failures,
        "article_count": sum(len(j["items"]) for j in journals_out),
        "notes": [
            "Order of 'journals' is the fixed order the issue must use.",
            "online_date is 'created' for Journal of Criminal Justice (items are in press) and the online publication date otherwise.",
            "Corrections, errata, corrigenda, retractions, editorials, book reviews and Campbell protocols are listed under 'dropped', not 'items'.",
            "open_access is true only when a Creative Commons licence URL is present; text-and-data-mining licences do not count.",
            "Each item's abstract_url holds the full abstract (JATS tags stripped) or null when the publisher deposits none (Elsevier and Taylor & Francis usually deposit none).",
        ],
        "journals": journals_out,
    }
    with open(out_dir / "index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    write_markdown(index, out_dir / "index.md")
    log(f"wrote {out_dir}/index.json: {index['article_count']} articles, {failures} journal(s) failed")
    # Exit non-zero only if everything failed, so a partial pull still gets committed.
    return 1 if failures == len(JOURNALS) else 0


if __name__ == "__main__":
    sys.exit(main())
