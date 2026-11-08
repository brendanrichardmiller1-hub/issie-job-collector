#!/usr/bin/env python3
"""
Daily job collector.

Runs on GitHub Actions every morning. Pulls recent London marketing jobs from:
  - LinkedIn, Indeed, Glassdoor, Google Jobs (via the open-source JobSpy library, no login)
  - Reed's official API (REED_API_KEY)
  - Adzuna's official API (ADZUNA_APP_ID / ADZUNA_APP_KEY), which aggregates Totaljobs,
    CV-Library, company sites and more
  - Employers' own Greenhouse / Lever / Ashby job boards (the career-ops approach)

Writes:
  data/latest.json     every job that passed the filters, newest first, with descriptions
  data/latest.md       a short human-readable list
  data/run-report.md   what each source returned or why it failed
  data/seen.json       when each job was first seen (so "new today" works even without dates)
  data/archive/YYYY-MM-DD.json  daily copy, pruned after 30 days

Every source is independent: one failing never stops the others.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ARCHIVE = DATA / "archive"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DESC_LIMIT = 6000
TODAY = dt.datetime.now(dt.timezone.utc).date()

report: list[str] = []


def redact(msg: str) -> str:
    """Never let API keys reach the (public) run report or logs."""
    msg = re.sub(r"(app_key|app_id|api_key|key|token)=[^&\s]+", r"\1=REDACTED", str(msg), flags=re.I)
    for name in ("REED_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        val = os.environ.get(name)
        if val:
            msg = msg.replace(val, "REDACTED")
    return msg


def log(msg: str) -> None:
    msg = redact(msg)
    print(msg, flush=True)
    report.append(msg)


# --------------------------------------------------------------------------- helpers

def norm(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


COMPANY_NOISE = {"ltd", "limited", "plc", "llp", "inc", "uk", "group", "the", "co"}


def job_key(company: str | None, title: str | None) -> str:
    c = " ".join(w for w in norm(company).split() if w not in COMPANY_NOISE)
    t = norm(title)
    t = re.sub(r"\b(london|hybrid|remote|uk|permanent|full time)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return f"{c}|{t}"


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def to_date(v) -> str | None:
    """Return YYYY-MM-DD or None from many input shapes."""
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)):  # epoch ms (Lever)
        return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return None


def money(v) -> float | None:
    try:
        f = float(v)
        return f if f == f and f > 0 else None
    except (TypeError, ValueError):
        return None


def fmt_salary(lo, hi) -> str:
    if lo and hi and round(lo) != round(hi):
        return f"£{lo:,.0f}–£{hi:,.0f}"
    if lo or hi:
        return f"£{(lo or hi):,.0f}"
    return ""


def get_json(url, **kw):
    kw.setdefault("timeout", 30)
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    r = requests.get(url, headers=headers, **kw)
    r.raise_for_status()
    return r.json()


def job(**f) -> dict:
    base = dict(
        source="", title="", company="", location="", salary_min=None, salary_max=None,
        salary_text="", salary_is_estimate=False, work_pattern="", contract="",
        date_posted=None, url="", description="",
    )
    base.update(f)
    for k, v in list(base.items()):
        if isinstance(v, float) and v != v:
            base[k] = None
    for k in ("title", "company", "location", "salary_text", "work_pattern", "contract", "url", "description"):
        base[k] = "" if base[k] is None else str(base[k])
    base["description"] = base["description"][:DESC_LIMIT]
    return base


# --------------------------------------------------------------------------- sources

def src_jobspy(cfg) -> list[dict]:
    from jobspy import scrape_jobs  # imported here so a broken install only kills this source

    sites = [s for s in ("linkedin", "indeed", "glassdoor", "google") if cfg["sources"].get(s)]
    if not sites:
        return []
    out: list[dict] = []
    per_site: dict[str, int] = {s: 0 for s in sites}
    errors: dict[str, str] = {}
    for term in cfg["search_terms"]:
        for site in sites:
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=term,
                    google_search_term=f"{term} jobs near London, UK since yesterday",
                    location=cfg["location"],
                    distance=cfg["radius_miles"],
                    results_wanted=40,
                    hours_old=cfg["hours_old"],
                    country_indeed="UK",
                    description_format="markdown",
                    fetch_description=(site == "linkedin"),
                    verbose=0,
                )
            except Exception as e:  # noqa: BLE001
                errors[site] = f"{type(e).__name__}: {e}"[:300]
                continue
            for site_err, msg in (getattr(df, "attrs", {}) or {}).get("errors", {}).items():
                errors[site_err] = str(msg)[:300]
            for row in df.to_dict("records"):
                try:
                    # pandas fills blanks with NaN (a float); turn those into None
                    row = {k: (None if isinstance(v, float) and v != v else v) for k, v in row.items()}
                    loc = row.get("location")
                    if isinstance(loc, dict):
                        loc = ", ".join(str(x) for x in loc.values() if x)
                    interval = str(row.get("interval") or "").lower()
                    lo, hi = money(row.get("min_amount")), money(row.get("max_amount"))
                    if lo or hi:
                        if "hour" in interval:
                            lo, hi = (lo and lo * 37.5 * 52), (hi and hi * 37.5 * 52)
                        elif "day" in interval:
                            lo, hi = (lo and lo * 230), (hi and hi * 230)
                        elif "month" in interval:
                            lo, hi = (lo and lo * 12), (hi and hi * 12)
                        elif "week" in interval:
                            lo, hi = (lo and lo * 52), (hi and hi * 52)
                    wp = row.get("work_from_home_type") or ("Remote" if row.get("is_remote") else "")
                    jt = row.get("job_type")
                    out.append(job(
                        source=row.get("site") or site,
                        title=row.get("title") or "",
                        company=row.get("company") or "",
                        location=str(loc or ""),
                        salary_min=lo, salary_max=hi,
                        salary_text=fmt_salary(lo, hi),
                        salary_is_estimate=str(row.get("salary_source") or "").lower() not in ("", "direct_data", "description"),
                        work_pattern=str(wp or ""),
                        contract=", ".join(map(str, jt)) if isinstance(jt, list) else str(jt or ""),
                        date_posted=to_date(row.get("date_posted")),
                        url=row.get("job_url") or "",
                        apply_url=row.get("job_url_direct") or "",
                        description=row.get("description") or "",
                        company_size=row.get("company_num_employees") or "",
                        company_industry=row.get("company_industry") or "",
                    ))
                    per_site[row.get("site") or site] = per_site.get(row.get("site") or site, 0) + 1
                except Exception as e:  # noqa: BLE001
                    errors.setdefault(site, f"row skipped: {type(e).__name__}: {e}"[:300])
            time.sleep(2)  # be gentle; LinkedIn rate-limits fast bursts
    for s in sites:
        line = f"- **{s}**: {per_site.get(s, 0)} raw results"
        if s in errors:
            line += f" — error: {errors[s]}"
        log(line)
    return out


def src_reed(cfg) -> list[dict]:
    key = os.environ.get("REED_API_KEY")
    if not key:
        log("- **reed**: skipped (no REED_API_KEY secret yet)")
        return []
    auth = (key, "")
    seen_ids, out = set(), []
    cutoff = TODAY - dt.timedelta(days=2)
    for term in cfg["search_terms"]:
        try:
            data = get_json(
                "https://www.reed.co.uk/api/1.0/search",
                params=dict(keywords=term, locationName="London",
                            distanceFromLocation=cfg["radius_miles"], resultsToTake=100),
                auth=auth,
            )
        except Exception as e:  # noqa: BLE001
            log(f"- **reed**: search '{term}' failed — {e}")
            continue
        for r in data.get("results", []):
            jid = r.get("jobId")
            posted = to_date(r.get("date"))
            if jid in seen_ids or (posted and posted < cutoff.isoformat()):
                continue
            seen_ids.add(jid)
            desc, contract, wp = r.get("jobDescription") or "", "", ""
            try:
                d = get_json(f"https://www.reed.co.uk/api/1.0/jobs/{jid}", auth=auth)
                desc = strip_html(d.get("jobDescription")) or desc
                contract = d.get("contractType") or ""
                if d.get("partTime") and not d.get("fullTime"):
                    contract += " (part-time)"
                posted = to_date(d.get("datePosted")) or posted
            except Exception:  # noqa: BLE001
                pass
            lo, hi = money(r.get("minimumSalary")), money(r.get("maximumSalary"))
            out.append(job(
                source="reed", title=r.get("jobTitle") or "", company=r.get("employerName") or "",
                location=r.get("locationName") or "", salary_min=lo, salary_max=hi,
                salary_text=fmt_salary(lo, hi),
                contract=contract, work_pattern=wp, date_posted=posted,
                url=r.get("jobUrl") or f"https://www.reed.co.uk/jobs/{jid}", description=desc,
            ))
            time.sleep(0.3)
    log(f"- **reed**: {len(out)} jobs posted in the last 2 days")
    return out


def src_adzuna(cfg) -> list[dict]:
    """One combined search per run (Adzuna's free tier is only 25 calls in total)."""
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log("- **adzuna**: skipped (no ADZUNA_APP_ID / ADZUNA_APP_KEY secrets yet)")
        return []
    pages = int(cfg.get("adzuna_pages", 1))
    out = []
    for page in range(1, pages + 1):
        try:
            data = get_json(
                f"https://api.adzuna.com/v1/api/jobs/gb/search/{page}",
                params={"app_id": app_id, "app_key": app_key,
                        "what_or": "marketing brand communications content campaign",
                        "where": "London", "distance": int(cfg["radius_miles"] * 1.6),
                        "max_days_old": 1, "results_per_page": 50, "sort_by": "date",
                        "content-type": "application/json"},
            )
        except Exception as e:  # noqa: BLE001
            log(f"- **adzuna**: page {page} failed — {e}")
            break
        results = data.get("results", [])
        for r in results:
            lo, hi = money(r.get("salary_min")), money(r.get("salary_max"))
            out.append(job(
                source="adzuna", title=strip_html(r.get("title")), company=(r.get("company") or {}).get("display_name", ""),
                location=(r.get("location") or {}).get("display_name", ""), salary_min=lo, salary_max=hi,
                salary_text=fmt_salary(lo, hi), salary_is_estimate=str(r.get("salary_is_predicted")) == "1",
                contract=" ".join(x for x in (r.get("contract_time"), r.get("contract_type")) if x),
                date_posted=to_date(r.get("created")),
                url=re.sub(r"[?&]utm_source=[^&]*", "", r.get("redirect_url") or ""),
                description=strip_html(r.get("description")),
            ))
        if len(results) < 50:
            break
    log(f"- **adzuna**: {len(out)} results from the last day ({pages} call(s) max per run)")
    return out


def src_ats(cfg) -> list[dict]:
    out, dead, ok = [], [], 0
    for b in cfg.get("ats_boards") or []:
        prov, board, company = b["provider"], b["board"], b["company"]
        try:
            if prov == "greenhouse":
                data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
                for j in data.get("jobs", []):
                    out.append(job(source=f"{company} careers", title=j.get("title"), company=company,
                                   location=(j.get("location") or {}).get("name", ""),
                                   date_posted=to_date(j.get("first_published") or j.get("updated_at")),
                                   url=j.get("absolute_url"), description=strip_html(j.get("content"))))
            elif prov == "lever":
                data = get_json(f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"})
                for j in data:
                    cat = j.get("categories") or {}
                    out.append(job(source=f"{company} careers", title=j.get("text"), company=company,
                                   location=cat.get("location", ""), contract=cat.get("commitment", ""),
                                   work_pattern=j.get("workplaceType", ""), date_posted=to_date(j.get("createdAt")),
                                   url=j.get("hostedUrl"), description=j.get("descriptionPlain") or strip_html(j.get("description"))))
            elif prov == "ashby":
                data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}", params={"includeCompensation": "true"})
                for j in data.get("jobs", []):
                    comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
                    out.append(job(source=f"{company} careers", title=j.get("title"), company=company,
                                   location=j.get("location", ""), salary_text=comp,
                                   work_pattern=j.get("workplaceType") or ("Remote" if j.get("isRemote") else ""),
                                   contract=j.get("employmentType", ""), date_posted=to_date(j.get("publishedAt")),
                                   url=j.get("jobUrl"), description=j.get("descriptionPlain") or ""))
            else:
                dead.append(f"{company} (unknown provider {prov})")
                continue
            ok += 1
        except Exception as e:  # noqa: BLE001
            dead.append(f"{company} ({prov}/{board}: {str(e)[:80]})")
        time.sleep(0.3)
    log(f"- **employer job boards**: {ok} boards read, {len(out)} open roles in total (before filtering)")
    if dead:
        log(f"  - boards that failed (check the name in config.yml): {'; '.join(dead)}")
    return out


# --------------------------------------------------------------------------- filtering

def passes(j: dict, cfg) -> str | None:
    """Return None if the job passes, else the reason it was dropped."""
    t = (j.get("title") or "").lower()
    if not t:
        return "no title"
    if not any(w in t for w in cfg["title_include"]):
        return "title"
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in cfg["title_exclude"]):
        return "title"
    loc = (j.get("location") or "").lower()
    if loc and not any(w in loc for w in cfg["location_include"]):
        return "location"
    top = j.get("salary_max") or j.get("salary_min")
    if top and not j.get("salary_is_estimate") and top < cfg["min_salary_gbp"] and top > 5000:
        return "salary"
    return None


def merge(jobs: list[dict]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for j in jobs:
        k = job_key(j["company"], j["title"])
        if k not in merged:
            j = dict(j)
            j["key"] = k
            j["sources"] = [j.pop("source")]
            j["other_urls"] = []
            merged[k] = j
            continue
        m = merged[k]
        if j["source"] not in m["sources"]:
            m["sources"].append(j["source"])
        if j["url"] and j["url"] != m["url"] and j["url"] not in m["other_urls"]:
            m["other_urls"].append(j["url"])
        # keep the richest value for each field
        if len(j.get("description") or "") > len(m.get("description") or ""):
            m["description"] = j["description"]
        for f in ("salary_min", "salary_max", "salary_text", "work_pattern", "contract", "location"):
            if not m.get(f) and j.get(f):
                m[f] = j[f]
        if j.get("date_posted") and (not m.get("date_posted") or j["date_posted"] > m["date_posted"]):
            m["date_posted"] = j["date_posted"]
        if m.get("salary_is_estimate") and j.get("salary_min") and not j.get("salary_is_estimate"):
            m.update(salary_min=j["salary_min"], salary_max=j["salary_max"],
                     salary_text=j["salary_text"], salary_is_estimate=False)
    return merged


# --------------------------------------------------------------------------- main

def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    DATA.mkdir(exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)
    started = dt.datetime.now(dt.timezone.utc)
    log(f"# Run report — {started:%Y-%m-%d %H:%M} UTC\n")
    log("## Sources")

    raw: list[dict] = []
    for name, fn in (("jobspy", src_jobspy), ("reed", src_reed), ("adzuna", src_adzuna), ("ats", src_ats)):
        if name == "jobspy" or cfg["sources"].get(name):
            try:
                raw += fn(cfg)
            except Exception as e:  # noqa: BLE001
                log(f"- **{name}**: crashed — {type(e).__name__}: {e}")
                traceback.print_exc()

    dropped: dict[str, int] = {}
    kept = []
    for j in raw:
        why = passes(j, cfg)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            kept.append(j)
    merged = merge(kept)

    # first-seen tracking
    seen_path = DATA / "seen.json"
    bootstrap = not seen_path.exists()
    seen: dict[str, str] = json.loads(seen_path.read_text()) if not bootstrap else {}
    window_start = (TODAY - dt.timedelta(days=1)).isoformat()
    for k, j in merged.items():
        first = seen.get(k)
        j["first_seen"] = first or TODAY.isoformat()
        is_new_key = first is None
        if j.get("date_posted"):
            # posted in the window AND not already collected on an earlier day
            fresh = j["date_posted"] >= window_start and (first is None or first >= TODAY.isoformat())
        else:
            fresh = is_new_key and not bootstrap
        if bootstrap and all(s.endswith(" careers") for s in j["sources"]):
            fresh = bool(j.get("date_posted") and j["date_posted"] >= window_start)
        j["is_new"] = bool(fresh)
        seen.setdefault(k, TODAY.isoformat())
    cutoff = (TODAY - dt.timedelta(days=90)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    jobs = sorted(merged.values(), key=lambda j: (not j["is_new"], j.get("date_posted") or "", j["title"]), reverse=False)
    jobs.sort(key=lambda j: (j["is_new"], j.get("date_posted") or ""), reverse=True)

    for j in jobs:  # keep the repo small: only new jobs carry full descriptions
        if not j["is_new"]:
            j["description"] = ""

    payload = {
        "generated_at": started.isoformat(),
        "window_start": window_start,
        "counts": {"raw": len(raw), "after_filters": len(merged),
                   "new_in_window": sum(j["is_new"] for j in jobs), "dropped": dropped},
        "jobs": jobs,
    }
    (DATA / "latest.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str))
    compact = [{k: v for k, v in j.items() if k != "description"} for j in jobs if j["is_new"]]
    (ARCHIVE / f"{TODAY}.json").write_text(json.dumps(compact, ensure_ascii=False, default=str))
    seen_path.write_text(json.dumps(seen, indent=0, sort_keys=True))
    for f in ARCHIVE.glob("*.json"):
        if f.stem < (TODAY - dt.timedelta(days=30)).isoformat():
            f.unlink()

    lines = [f"# Jobs collected {TODAY} ({payload['counts']['new_in_window']} new)\n"]
    for j in jobs:
        if not j["is_new"]:
            continue
        sal = j["salary_text"] or "salary not stated"
        if j.get("salary_is_estimate") and j["salary_text"]:
            sal += " (estimate)"
        lines.append(f"- **{j['title']}** — {j['company'] or '?'} — {sal} — {j['location']} — "
                     f"posted {j.get('date_posted') or '?'} — {', '.join(j['sources'])} — {j['url']}")
    (DATA / "latest.md").write_text("\n".join(lines) + "\n")

    log("\n## Totals")
    log(f"- raw results: {len(raw)}")
    log(f"- dropped by filters: {dropped}")
    log(f"- unique jobs after filters: {len(merged)}")
    log(f"- new in the last ~24h: {payload['counts']['new_in_window']}")
    if bootstrap:
        log("- first run: employer-board roles without a posting date were treated as old")
    (DATA / "run-report.md").write_text("\n".join(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
