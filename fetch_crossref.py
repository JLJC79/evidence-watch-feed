#!/usr/bin/env python3
"""Weekly Crossref pull for the Threshold Research Group 'Evidence & Policy Watch'.

Pulls recent journal-article records for the 14 tracked journals from the Crossref REST API
(polite pool) and writes them to a static folder that GitHub Pages serves, so the weekly
issue-building run makes one fetch of data/index.json instead of twenty-odd calls to
api.crossref.org through a rate-limited proxy.

Since October 2026 it also pulls the Australian Institute of Criminology's publications by
DOI prefix (10.52922): the AIC's own listing pages are stale or robots-blocked, but every AIC
publication is registered with Crossref, with its abstract, usually on the day of release.
These go under a separate 'reports' key so the 'journals' array keeps its fixed 14 entries.

Since 23 November 2026 it also writes a compact 21-day "window" listing (window.md and
window.json) that the issue run reads first. It is about a quarter of the size of index.md,
so it fits one fetch when the run has to read through a subagent, and it carries the
abstract file name for every item. The 35-day index files are unchanged. Campbell
Systematic Reviews items on non-crime topics are now dropped by keyword (listed under
'dropped: out of scope' so the exclusion can be audited); see CAMPBELL_EXCLUDE below.

Outputs (under --out, default ./data):
  index.json            'journals' (one entry per journal, fixed ISSN order) and 'reports'
                        (one entry per report publisher), with compact item records
  index.md              the same listing as readable markdown (for checking by eye)
  window.json           the 21-day subset of index.json (no 'dropped' lists)
  window.md             the 21-day subset as markdown, one line per item, with abstract file
  abstracts/<slug>.json one file per item carrying the full abstract (JATS tags stripped)

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
    ("Criminal Justice and Behavior", "0093-8548", "published"),
    ("Sexual Abuse", "1079-0632", "published"),
    ("Journal of Sexual Aggression", "1355-2600", "published"),
    ("Trauma, Violence, & Abuse", "1524-8380", "published"),
    ("Campbell Systematic Reviews", "1891-1803", "published"),
]

# Report publishers pulled by Crossref DOI prefix rather than ISSN. The AIC registers a DOI for
# every publication (Trends & issues, Research Reports, Statistical Bulletins, CRG reports,
# Special reports ...); the Crossref 'created' date is the registration date, which has tracked
# the AIC's own publication date within a day in the items checked, so it is used as the item
# date and labelled as such.
REPORT_SOURCES = [
    ("Australian Institute of Criminology", "10.52922"),
]

# Series name from the letters at the start of an AIC DOI suffix (10.52922/ti78458 -> 'ti').
AIC_SERIES = {
    "ti": "Trends & issues in crime and criminal justice",
    "rr": "Research Report",
    "sb": "Statistical Bulletin",
    "sr": "Statistical Report",
    "crf": "CRG report",
    "sp": "Special report",
    "rip": "Research in practice",
    "tbp": "Technical and background paper",
    "rpp": "Research and public policy series",
}

# Titles starting with these are dropped (and listed under 'dropped' so nothing vanishes silently).
DROP_PREFIXES = ("correction", "erratum", "corrigendum", "retraction", "editorial", "book review", "issue information")
# Campbell Systematic Reviews: protocols are dropped too.
PROTOCOL_ISSN = "1891-1803"

# Campbell Systematic Reviews publishes across health, education, international development and
# social welfare as well as crime and justice. A Campbell title is dropped as 'out of scope' when
# it contains one of CAMPBELL_EXCLUDE and none of CAMPBELL_KEEP (the keep list wins, so a review
# of, say, health care in prisons stays). Both lists are matched case-insensitively against the
# title. Edit these two lists to tune the filter; dropped titles are still listed in index.md.
CAMPBELL_EXCLUDE = (
    "health", "medical", "medicine", "clinical", "nursing", "nurses", "patient", "disease",
    "cancer", "diabetes", "obesity", "nutrition", "vaccin", "pharmac", "surgery", "dialysis",
    "kidney", "disabling conditions", "self-management", "rehabilitation to improve",
    "age-friendly", "older adult", "older people", "ageing", "aging", "dementia",
    "informed consent", "allyship", "anti-racism", "higher education", "classroom", "literacy",
    "numeracy", "teacher", "early childhood education", "agricultur", "farmer", "cash transfer",
    "microfinance", "sanitation", "drinking water", "humanitarian", "refugee camp",
    "data extraction", "arm-level", "numerical results", "military personnel", "veterans",
)
CAMPBELL_KEEP = (
    "crime", "criminal", "offend", "reoffend", "recidiv", "delinquen", "violence", "violent",
    "abuse", "maltreatment", "victim", "police", "policing", "prison", "jail", "custod",
    "detention", "court", "sentenc", "probation", "parole", "justice", "gang", "bullying",
    "harassment", "trafficking", "firearm", "gun", "drug", "alcohol", "homicide", "terror",
    "extremis", "radicali", "corruption", "fraud", "cyber", "restorative", "diversion",
)

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
    if issn == PROTOCOL_ISSN:
        if t.startswith("protocol") or "protocol:" in t[:40] or t.endswith("protocol"):
            return "protocol"
        if any(k in t for k in CAMPBELL_EXCLUDE) and not any(k in t for k in CAMPBELL_KEEP):
            return "out of scope"
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


def clean_title(rec: dict) -> str:
    """First title, whitespace-normalised, HTML entities decoded (AIC deposits '&amp;')."""
    return html.unescape(" ".join((rec.get("title") or [""])[0].split()))


def build_item(rec: dict, journal: str, issn: str, sort_key: str, base_url: str, out_dir: Path) -> dict:
    doi = rec.get("DOI", "")
    title = clean_title(rec)
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


def aic_series(doi: str) -> str | None:
    """'10.52922/ti78458' -> 'Trends & issues in crime and criminal justice' (None if unrecognised)."""
    m = re.match(r"^10\.52922/([a-z]+)\d", doi.lower())
    return AIC_SERIES.get(m.group(1)) if m else None


def build_report_item(rec: dict, publisher: str, prefix: str, base_url: str, out_dir: Path) -> dict:
    """Like build_item, for a report publisher. The item date is the Crossref registration date."""
    doi = rec.get("DOI", "")
    title = clean_title(rec)
    names = author_names(rec.get("author"))
    created = date_time(rec.get("created"))
    published = date_parts(rec.get("published-online")) or date_parts(rec.get("published"))
    series = aic_series(doi) if prefix == "10.52922" else None
    container = " ".join((rec.get("container-title") or [""])[0].split()) or None
    abstract = clean_abstract(rec.get("abstract"))
    slug = slug_for(doi)
    abstract_rel = f"abstracts/{slug}.json"
    item = {
        "doi": doi,
        "url": f"https://doi.org/{doi}",
        "title": title,
        "publisher": publisher,
        "series": series or container,
        "crossref_type": rec.get("type"),
        "authors": names,
        "author_label": author_label(names),
        "published": published,
        "created": created,
        "online_date": created,            # registration date: the reliable, day-level date for AIC
        "date_label": display_date(created),
        "meta_date": long_date(created),
        "date_basis": "Crossref DOI registration date",
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


def fetch_prefix(name: str, prefix: str, since: str, mailto: str, rows: int) -> dict:
    """All works registered under a DOI prefix since `since` (no type filter: AIC uses several)."""
    params = {
        "filter": f"prefix:{prefix},from-created-date:{since}",
        "sort": "created",
        "order": "desc",
        "rows": str(rows),
        "select": SELECT,
        "mailto": mailto,
    }
    url = API + "?" + urllib.parse.urlencode(params, safe=":,-")
    log(f"{name} (prefix {prefix}) sort=created")
    data = get_json(url, mailto)
    msg = data.get("message", {})
    return {"items": msg.get("items", []), "total": msg.get("total-results", 0), "query": url}


def write_markdown(index: dict, path: Path) -> None:
    lines = [f"# Crossref pull — generated {index['generated_at_brisbane']} (Brisbane)",
             "", f"Window: articles dated on or after {index['window_from']} ({index['window_days']} days). "
             "Items in the issue's 21-day window are a subset; apply the window at build time using `online_date`. "
             "The 21-day subset is also published as window.md and window.json.", ""]
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
    if index.get("reports"):
        lines.append("# Reports (by DOI prefix; dates are Crossref registration dates)")
        lines.append("")
        for r in index["reports"]:
            status = f"{len(r['items'])} item(s)" if r["ok"] else f"FAILED: {r['error']}"
            lines.append(f"## {r['name']} — {status}")
            for it in r["items"]:
                oa = " · open access" if it["open_access"] else ""
                ab = "" if it["has_abstract"] else " · no abstract"
                series = f" · {it['series']}" if it.get("series") else ""
                lines.append(f"- {it['title']} — {it['author_label']}{series} · {it['date_label']}{oa}{ab} · https://doi.org/{it['doi']}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def in_window(item: dict, cutoff: str) -> bool:
    """True when the item's online_date is on or after the cutoff ('YYYY-MM-DD'). An item whose date
    is known only to the month or year is kept, so nothing is lost for want of a day."""
    d = item.get("online_date") or ""
    if len(d) < 10:
        return True
    return d >= cutoff


def build_window(index: dict, issue_days: int, today: dt.date) -> dict:
    """The issue's own window: items dated within the last `issue_days` days, same structure as
    index.json but without the 'dropped' lists and with the abstract file name on each item."""
    cutoff = (today - dt.timedelta(days=issue_days)).isoformat()

    def trim(it: dict) -> dict:
        out = dict(it)
        out["abstract_file"] = f"abstracts/{slug_for(it['doi'])}.json"
        return out

    journals = []
    for j in index["journals"]:
        items = [trim(it) for it in j["items"] if in_window(it, cutoff)]
        journals.append({"name": j["name"], "issn": j["issn"], "sort": j["sort"], "ok": j["ok"],
                         "error": j["error"], "items": items})
    reports = []
    for r in index.get("reports", []):
        items = [trim(it) for it in r["items"] if in_window(it, cutoff)]
        reports.append({"name": r["name"], "prefix": r["prefix"], "ok": r["ok"], "error": r["error"], "items": items})
    return {
        "generated_at_utc": index["generated_at_utc"],
        "generated_at_brisbane": index["generated_at_brisbane"],
        "window_from": cutoff,
        "window_days": issue_days,
        "journal_count": len(journals),
        "article_count": sum(len(j["items"]) for j in journals),
        "report_count": sum(len(r["items"]) for r in reports),
        "notes": [
            f"Subset of index.json: items whose online_date is on or after {cutoff} ({issue_days} days before the pull).",
            "Order of 'journals' is the fixed order the issue must use; a journal with no items in the window still appears.",
            "abstract_file is the path under data/ of the item's abstract file (abstract_url is the same file as a full URL).",
            "Items dropped as corrections, editorials, protocols or out-of-scope Campbell reviews are not listed here; see index.md.",
        ],
        "journals": journals,
        "reports": reports,
    }


def write_window_markdown(window: dict, path: Path) -> None:
    lines = [f"# Issue window — generated {window['generated_at_brisbane']} (Brisbane)",
             "", f"Items dated on or after {window['window_from']} ({window['window_days']} days). "
             f"{window['article_count']} article(s) across {window['journal_count']} journals; "
             f"{window['report_count']} report(s). Each line: title — author label · date · flags · DOI · abstract file.", ""]
    for j in window["journals"]:
        status = f"{len(j['items'])} item(s)" if j["ok"] else f"FAILED: {j['error']}"
        lines.append(f"## {j['name']} — {status}")
        if not j["items"] and j["ok"]:
            lines.append("- (none in window)")
        for it in j["items"]:
            oa = " · open access" if it["open_access"] else ""
            ab = " · abstract" if it["has_abstract"] else " · no abstract"
            lines.append(f"- {it['title']} — {it['author_label']} · {it['date_label']}{oa}{ab} · https://doi.org/{it['doi']} · {it['abstract_file']}")
        lines.append("")
    if window.get("reports"):
        lines.append("# Reports (dates are Crossref registration dates)")
        lines.append("")
        for r in window["reports"]:
            status = f"{len(r['items'])} item(s)" if r["ok"] else f"FAILED: {r['error']}"
            lines.append(f"## {r['name']} — {status}")
            if not r["items"] and r["ok"]:
                lines.append("- (none in window)")
            for it in r["items"]:
                oa = " · open access" if it["open_access"] else ""
                ab = " · abstract" if it["has_abstract"] else " · no abstract"
                series = f" · {it['series']}" if it.get("series") else ""
                lines.append(f"- {it['title']} — {it['author_label']}{series} · {it['date_label']}{oa}{ab} · https://doi.org/{it['doi']} · {it['abstract_file']}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--mailto", required=True)
    ap.add_argument("--days", type=int, default=35, help="look-back window in days (wider than the issue's 21)")
    ap.add_argument("--issue-days", type=int, default=21, help="the issue's own window, for window.md/window.json")
    ap.add_argument("--rows", type=int, default=100)
    ap.add_argument("--base-url", default="", help="public URL of the out folder, e.g. https://user.github.io/repo/data")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between Crossref requests")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")
    # GitHub passes the repository owner with its original capitalisation; hostnames are
    # case-insensitive, but keep the published URLs tidy.
    m = re.match(r"^(https?://)([^/]+)(.*)$", base_url)
    if m:
        base_url = m.group(1) + m.group(2).lower() + m.group(3)

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
                title = clean_title(rec)
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

    reports_out = []
    report_failures = 0
    for name, prefix in REPORT_SOURCES:
        entry = {"name": name, "prefix": prefix, "sort": "created", "ok": False, "error": None,
                 "total_results": 0, "items": []}
        try:
            res = fetch_prefix(name, prefix, since, args.mailto, args.rows)
            entry["ok"] = True
            entry["total_results"] = res["total"]
            for rec in res["items"]:
                entry["items"].append(build_report_item(rec, name, prefix, base_url, out_dir))
        except Exception as e:
            report_failures += 1
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"  FAILED: {entry['error']}")
        reports_out.append(entry)
        time.sleep(args.pause)

    index = {
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_brisbane": brisbane.strftime("%Y-%m-%d %H:%M AEST"),
        "window_from": since,
        "window_days": args.days,
        "journal_count": len(journals_out),
        "journals_failed": failures,
        "article_count": sum(len(j["items"]) for j in journals_out),
        "report_source_count": len(reports_out),
        "report_sources_failed": report_failures,
        "report_count": sum(len(r["items"]) for r in reports_out),
        "notes": [
            "Order of 'journals' is the fixed order the issue must use.",
            "online_date is 'created' for Journal of Criminal Justice (items are in press) and the online publication date otherwise.",
            "Corrections, errata, corrigenda, retractions, editorials, book reviews, issue-information pages, Campbell protocols and out-of-scope Campbell reviews are listed under 'dropped', not 'items'.",
            "open_access is true only when a Creative Commons licence URL is present; text-and-data-mining licences do not count.",
            "Each item's abstract_url holds the full abstract (JATS tags stripped) or null when the publisher deposits none (Elsevier and Taylor & Francis usually deposit none).",
            "'reports' holds publishers pulled by DOI prefix (currently the Australian Institute of Criminology, 10.52922). Their online_date is the Crossref DOI registration date, which for the AIC has matched its publication date within a day; the issue should say 'DOI registered <date>' or verify the date on the item's own aic.gov.au page.",
            "window.json and window.md hold the subset dated within the issue's own window (default 21 days).",
        ],
        "journals": journals_out,
        "reports": reports_out,
    }
    with open(out_dir / "index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    write_markdown(index, out_dir / "index.md")

    window = build_window(index, args.issue_days, now_utc.date())
    with open(out_dir / "window.json", "w", encoding="utf-8") as f:
        json.dump(window, f, ensure_ascii=False, indent=1)
    write_window_markdown(window, out_dir / "window.md")

    log(f"wrote {out_dir}/index.json: {index['article_count']} articles, {failures} journal(s) failed; "
        f"{index['report_count']} report(s), {report_failures} report source(s) failed; "
        f"window: {window['article_count']} articles, {window['report_count']} reports")
    # Exit non-zero only if everything failed, so a partial pull still gets committed.
    return 1 if failures == len(JOURNALS) else 0


if __name__ == "__main__":
    sys.exit(main())
