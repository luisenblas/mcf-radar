#!/usr/bin/env python3
"""MyCareersFuture (Singapore) job radar and salary history.

Unofficial. Pulls the public JSON API that the MyCareersFuture website uses,
appends every new posting (salary max >= floor) to one historical CSV, scores
relevance with a deterministic profile you control, and writes plain Markdown
and text reports. No LLM calls.

Usage:
  mcf_radar.py                 incremental run (new postings since last run)
  mcf_radar.py --backfill      one-off: pull every live posting above the floor
  mcf_radar.py --report        only re-render the reports from the CSV
  mcf_radar.py --max-details N cap detail fetches this run (default 800; backfill: unlimited)
  mcf_radar.py --out DIR       output directory (default ./data)
  mcf_radar.py --profile FILE  scoring profile (default profile.json, else profile.example.json)
"""
import csv, json, os, re, sys, time, argparse, statistics, urllib.request, urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- config
API = "https://api.mycareersfuture.gov.sg/v2"
PAGE = 100              # API hard max
SLEEP = 0.4             # politeness between requests (the API returns 429 when pushed harder)
WORKERS = 1             # sequential on purpose; parallel requests trigger 429 and an IP cooldown
HERE = Path(__file__).resolve().parent

FIELDS = ["first_seen", "posting_date", "original_posting_date", "expiry_date", "job_id", "uuid",
          "title", "company", "company_uen", "category", "position_level", "employment_type",
          "min_years_exp", "salary_min", "salary_max", "salary_type", "skills", "applications",
          "views", "repost_count", "status", "role_family", "relevance", "url"]

# Filled by load_profile(); see profile.example.json.
FLOOR = BAND = RELEVANT_MIN_SALARY_MAX = MAX_YEARS = RELEVANT_THRESHOLD = 0
FAMILIES = []
TARGET_FAMILIES = SHOW_FAMILIES_FOR_WATCHLIST = PREFERRED_FAMILIES = JUNIOR_LEVELS = SENIOR_LEVELS = set()
FAMILY_BONUS, W, WATCHLIST = {}, {}, {}
NEG_TITLE = AI_RE = AGENCY_RE = None
CSV_PATH = STATE = DIGEST = RADAR_NOTE = BENCH_NOTE = None

def load_profile(path):
    global FLOOR, BAND, RELEVANT_MIN_SALARY_MAX, MAX_YEARS, RELEVANT_THRESHOLD, FAMILIES
    global TARGET_FAMILIES, SHOW_FAMILIES_FOR_WATCHLIST, PREFERRED_FAMILIES, JUNIOR_LEVELS, SENIOR_LEVELS
    global FAMILY_BONUS, W, WATCHLIST, NEG_TITLE, AI_RE, AGENCY_RE
    p = json.loads(Path(path).read_text(encoding="utf-8"))
    FLOOR, BAND = p["floor"], p["band"]
    RELEVANT_MIN_SALARY_MAX, MAX_YEARS, RELEVANT_THRESHOLD = p["relevant_min_salary_max"], p["max_years"], p["threshold"]
    FAMILIES = [tuple(f) for f in p["families"]]
    TARGET_FAMILIES = set(p["target_families"])
    SHOW_FAMILIES_FOR_WATCHLIST = set(p["watchlist_families"])
    PREFERRED_FAMILIES = set(p["preferred_families"])
    JUNIOR_LEVELS, SENIOR_LEVELS = set(p["junior_levels"]), set(p["senior_levels"])
    FAMILY_BONUS, W = p["family_bonus"], p["weights"]
    WATCHLIST = {k: [n.lower() for n in p["watchlist"].get(k, [])] for k in ("tier1", "tier2", "agency")}
    NEG_TITLE = re.compile(p["negative_title"], re.I)
    AI_RE = re.compile(p["boost_keywords"], re.I)
    AGENCY_RE = re.compile(p["agency_pattern"], re.I)

# ---------------------------------------------------------------- http
HEADERS = {"Content-Type": "application/json", "Accept": "application/json",
           "Origin": "https://www.mycareersfuture.gov.sg", "Referer": "https://www.mycareersfuture.gov.sg/",
           "User-Agent": "mcf-radar/1.0 (unofficial personal tool)"}

class Gone(Exception): pass   # 404: posting removed

def http(url, body=None, tries=6):
    """Returns parsed JSON; raises Gone on 404; returns None after exhausting retries (caller leaves data blank)."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers=HEADERS, method="POST" if body is not None else "GET")
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404: raise Gone()
            wait = int(e.headers.get("Retry-After", 0) or 0) if e.code == 429 else 0
            time.sleep(max(wait, 10 * (i + 1)) if e.code == 429 else 3 * (i + 1))
        except Exception:
            time.sleep(3 * (i + 1))
    return None

def search_page(page):
    # NB: sortBy only works inside the body (as a list); the URL param is ignored.
    return http(f"{API}/search?limit={PAGE}&page={page}",
                {"search": "", "salary": FLOOR, "sessionId": "", "sortBy": ["new_posting_date"]})

def job_detail(uuid):
    return http(f"{API}/jobs/{uuid}")

# ---------------------------------------------------------------- helpers
def first(lst, key):
    return lst[0][key] if lst else ""

def load_rows():
    if not CSV.exists(): return []
    with CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def save_rows(rows):
    tmp = CSV.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    tmp.replace(CSV)

def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}

def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))


# ---------------------------------------------------------------- helpers
def first(lst, key):
    return lst[0][key] if lst else ""

def load_rows():
    if not CSV_PATH.exists(): return []
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def save_rows(rows):
    tmp = CSV_PATH.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    tmp.replace(CSV_PATH)

def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}

def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))

def company_tier(company):
    c = company.lower()
    for tier in ("agency", "tier1", "tier2"):
        for name in WATCHLIST[tier]:
            if re.search(r"(^|[\s(])" + re.escape(name) + r"(?![a-z])", c) if len(name) <= 4 else re.search(r"(^|[\s(])" + re.escape(name), c):
                return tier
    return "agency" if AGENCY_RE.search(c) else ""

def clean_co(name):
    n = re.sub(r"\b(pte\.?|ltd\.?|private|limited|singapore|inc\.?|llc|asia[- ]pacific|\(singapore\)|branch|holdings|co\.?|corporation|corp\.?)\b", "", name, flags=re.I)
    return re.sub(r"[\s,.()]+$", "", re.sub(r"\s+", " ", n)).strip().title() or name.title()

def role_family(title):
    for fam, rx in FAMILIES:
        if re.search(rx, title, re.I): return fam
    return "Other"


def relevance(row):
    """0-100 deterministic fit score, driven by the profile."""
    title, comp = row["title"], row["company"].lower()
    fam = row["role_family"]
    smax = int(float(row["salary_max"] or 0))
    yrs = row["min_years_exp"]
    yrs = int(yrs) if str(yrs).isdigit() else None
    if fam not in TARGET_FAMILIES: return 0
    if "Full Time" not in row["employment_type"] and "Permanent" not in row["employment_type"]: return 0
    if row["position_level"] in JUNIOR_LEVELS: return 0
    if NEG_TITLE.search(title): return 0
    if smax < RELEVANT_MIN_SALARY_MAX: return 0
    if yrs is not None and yrs > MAX_YEARS: return 0
    s = W["base"]
    if smax >= BAND: s += W["salary_band"]
    if smax >= W["high_salary_threshold"]: s += W["salary_high"]
    if row["position_level"] in SENIOR_LEVELS: s += W["senior_level"]
    lo, hi = W["years_sweet_spot_range"]
    if yrs is not None and lo <= yrs <= hi: s += W["years_sweet_spot"]
    if AI_RE.search(title) or AI_RE.search(comp): s += W["boost_keyword"]
    tier = company_tier(row["company"])
    if tier == "tier1": s += W["tier1"]
    elif tier == "tier2": s += W["tier2"]
    elif tier == "agency": s += W["agency"]
    if fam in PREFERRED_FAMILIES: s += W["preferred_family"]
    s += FAMILY_BONUS.get(fam, 0)
    return max(0, min(s, 100))

def row_from_search(r, today):
    sal = r.get("salary") or {}
    md = r.get("metadata", {})
    co = (r.get("postedCompany") or {}) if not r.get("hiringCompany") else r["hiringCompany"]
    return {
        "first_seen": today, "posting_date": md.get("newPostingDate", ""), "original_posting_date": "",
        "expiry_date": "", "job_id": md.get("jobPostId", ""), "uuid": r["uuid"], "title": r.get("title", "").strip(),
        "company": (co.get("name") or "").strip(), "company_uen": co.get("uen") or "",
        "category": "; ".join(c["category"] for c in r.get("categories", [])),
        "position_level": first(r.get("positionLevels", []), "position"),
        "employment_type": "; ".join(e["employmentType"] for e in r.get("employmentTypes", [])),
        "min_years_exp": "", "salary_min": sal.get("minimum", ""), "salary_max": sal.get("maximum", ""),
        "salary_type": (sal.get("type") or {}).get("salaryType", ""),
        "skills": "; ".join(s["skill"] for s in r.get("skills", [])[:12]),
        "applications": md.get("totalNumberJobApplication", ""), "views": "", "repost_count": "",
        "status": (r.get("status") or {}).get("jobStatus", ""), "role_family": role_family(r.get("title", "")),
        "relevance": "", "url": md.get("jobDetailsUrl", ""),
    }

def enrich(row, d):
    md = d.get("metadata", {})
    row["min_years_exp"] = d.get("minimumYearsExperience", "")
    row["original_posting_date"] = md.get("originalPostingDate", "")
    row["expiry_date"] = md.get("expiryDate", "")
    row["views"] = md.get("totalNumberOfView", "")
    row["repost_count"] = md.get("repostCount", "")
    if row["min_years_exp"] == "": row["min_years_exp"] = "?"   # fetched, but employer left it blank

# ---------------------------------------------------------------- collect
def collect(rows, backfill, since_date):
    """Walk newest-first pages; stop once a whole page is older than since_date (unless backfill)."""
    seen = {(r["uuid"], r["posting_date"]) for r in rows}
    today = date.today().isoformat()
    new, page = [], 0
    while True:
        d = search_page(page)
        if not d or not d.get("results"): break
        res = d["results"]
        for r in res:
            k = (r["uuid"], r.get("metadata", {}).get("newPostingDate", ""))
            if k in seen: continue
            seen.add(k); new.append(row_from_search(r, today))
        oldest = min(r["metadata"]["newPostingDate"] for r in res)
        page += 1
        if not backfill and oldest < since_date: break
        if page * PAGE >= d.get("total", 0): break
        time.sleep(SLEEP)
    return new

def fetch_details(rows, cap, workers=WORKERS):
    from concurrent.futures import ThreadPoolExecutor
    pending = [r for r in rows if r["min_years_exp"] == ""]
    pending.sort(key=lambda r: r["posting_date"], reverse=True)
    todo = pending[:cap] if cap else pending
    def one(r):
        try:
            d = job_detail(r["uuid"])
        except Gone:
            r["min_years_exp"] = "?"; return          # removed before we got to it; stop retrying
        if d: enrich(r, d)                             # else: leave blank, next run retries
        time.sleep(SLEEP)
    n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(one, todo):
            n += 1
            if n % 500 == 0: save_rows(rows); print(f"  details {n}/{len(todo)}", flush=True)
    return n, len(pending) - n

# ---------------------------------------------------------------- render
def money(v):
    try: return f"{int(float(v)):,}"
    except (TypeError, ValueError): return "?"

def yrs_bucket(y):
    if not str(y).isdigit(): return "n/a"
    y = int(y)
    return "0-2" if y <= 2 else "3-5" if y <= 5 else "6-8" if y <= 8 else "9-12" if y <= 12 else "13+"

def med(xs):
    return int(statistics.median(xs)) if xs else 0

def smax(r): return int(float(r["salary_max"] or 0))
def fit(r): return int(r["relevance"] or 0)

def watch_new_rows(new_rows, exclude_ids):
    return [r for r in new_rows if r["uuid"] not in exclude_ids and company_tier(r["company"]) in ("tier1", "tier2")
            and r["role_family"] in SHOW_FAMILIES_FOR_WATCHLIST and r["position_level"] not in JUNIOR_LEVELS
            and not NEG_TITLE.search(r["title"])]

def render_radar(rows, new_rows, today):
    def co(r):
        t = company_tier(r["company"])
        return clean_co(r["company"])[:34] + (" (agency)" if t == "agency" else " *" if t == "tier1" else "")
    def line(r):
        return (f"| {fit(r)} | [{r['title'][:70]}]({r['url']}) | {co(r)} | "
                f"{money(r['salary_min'])}-{money(r['salary_max'])} | {r['min_years_exp'] or '?'} | {r['position_level']} | "
                f"{r['posting_date']} | {r['expiry_date'] or '?'} | {r['applications']} |")
    hdr = ("| Fit | Role | Company | SGD/month | Yrs | Level | Posted | Expires | Apps |\n"
           "|---|---|---|---|---|---|---|---|---|\n")
    def table(items, cap=60):
        items = sorted(items, key=lambda r: (-fit(r), -smax(r)))[:cap]
        return (hdr + "\n".join(line(r) for r in items) + "\n\n") if items else "_none_\n\n"
    new_rel = [r for r in new_rows if fit(r) >= RELEVANT_THRESHOLD]
    band_new = [r for r in new_rel if smax(r) >= BAND]
    band_ids = {r["uuid"] for r in band_new}
    watch_new = watch_new_rows(new_rows, band_ids)
    watch_ids = {r["uuid"] for r in watch_new}
    other_new = [r for r in new_rel if r["uuid"] not in band_ids and r["uuid"] not in watch_ids]
    live = {}
    for r in sorted(rows, key=lambda r: r["posting_date"]):
        if fit(r) >= RELEVANT_THRESHOLD and smax(r) >= BAND and (not r["expiry_date"] or r["expiry_date"] >= today):
            live[r["uuid"]] = r
    live = list(live.values())
    return (f"# MCF Job Radar (Singapore)\n\nGenerated {today} from mycareersfuture.gov.sg. "
            f"Master record: `mcf_jobs.csv` ({len(rows):,} postings). "
            f"Band = salary max >= SGD {BAND:,}/month. * = tier 1 watchlist company. Fit is a deterministic score from your profile.\n\n"
            f"## New in your band since last run ({len(band_new)})\n\n" + table(band_new) +
            f"## New from watchlist companies, any role ({len(watch_new)})\n\n" + table(watch_new, 40) +
            f"## Other new relevant, {RELEVANT_MIN_SALARY_MAX//1000}k-{BAND//1000}k ({len(other_new)})\n\n" + table(other_new, 30) +
            f"## Open postings in your band ({len(live)})\n\n" + table(live, 100))

def render_digest(rows, new_rows, today):
    """Short plain text summary, no links."""
    def b(r):
        t = company_tier(r["company"])
        tag = " *" if t == "tier1" else " (agency)" if t == "agency" else ""
        return (f"- {r['title'][:60]} - {clean_co(r['company'])[:30]}{tag} | SGD {money(r['salary_min'])}-{money(r['salary_max'])} | "
                f"{r['min_years_exp'] or '?'} yrs | expires {r['expiry_date'] or '?'}")
    band_new = sorted([r for r in new_rows if fit(r) >= RELEVANT_THRESHOLD and smax(r) >= BAND], key=lambda r: (-fit(r), -smax(r)))
    watch_new = sorted(watch_new_rows(new_rows, {r["uuid"] for r in band_new}), key=lambda r: -smax(r))
    open_band = len({r["uuid"] for r in rows if fit(r) >= RELEVANT_THRESHOLD and smax(r) >= BAND and (not r["expiry_date"] or r["expiry_date"] >= today)})
    out = [f"MCF radar {today}: {len(band_new)} new in band (>={BAND//1000}k), {len(watch_new)} new at watchlist companies, {open_band} open in band."]
    out += [b(r) for r in band_new[:6]]
    if len(band_new) > 6: out.append(f"- +{len(band_new) - 6} more in band")
    if watch_new: out += ["Watchlist, other roles:"] + [b(r) for r in watch_new[:3]]
    return "\n".join(out) + "\n"

def render_bench(rows, today):
    rows = [r for r in rows if str(r["salary_max"]).replace('.','').isdigit()]
    fams = [f for f, _ in FAMILIES] + ["Other"]
    buckets = ["0-2", "3-5", "6-8", "9-12", "13+", "n/a"]
    def table_for(subset, label):
        out = [f"### {label} (n={len(subset):,})\n\n| Role family | Yrs exp | n | Median min | Median max | Median mid | p75 max |\n|---|---|---|---|---|---|---|\n"]
        for fam in fams:
            for b in buckets:
                g = [r for r in subset if r["role_family"] == fam and yrs_bucket(r["min_years_exp"]) == b]
                if len(g) < 3: continue
                mins = [int(float(r["salary_min"])) for r in g]; maxs = [int(float(r["salary_max"])) for r in g]
                mids = [(a + c) // 2 for a, c in zip(mins, maxs)]
                p75 = sorted(maxs)[int(len(maxs) * 0.75)]
                out.append(f"| {fam} | {b} | {len(g)} | {med(mins):,} | {med(maxs):,} | {med(mids):,} | {p75:,} |\n")
        return "".join(out) + "\n"
    target = [r for r in rows if r["role_family"] in TARGET_FAMILIES]
    lvl = ["### Target families by position level\n\n| Level | n | Median min | Median max |\n|---|---|---|---|\n"]
    for L in ["Senior Executive", "Professional", "Manager", "Middle Management", "Senior Management"]:
        g = [r for r in target if r["position_level"] == L]
        if g: lvl.append(f"| {L} | {len(g)} | {med([int(float(r['salary_min'])) for r in g]):,} | {med([int(float(r['salary_max'])) for r in g]):,} |\n")
    top = sorted({r["uuid"]: r for r in target if r["relevance"] and int(r["relevance"]) >= RELEVANT_THRESHOLD}.values(),
                 key=lambda r: -int(float(r["salary_max"])))[:25]
    toptab = ["### Top-paying relevant postings seen (all time)\n\n| SGD/month | Role | Company | Yrs | Posted |\n|---|---|---|---|---|\n"]
    toptab += [f"| {money(r['salary_min'])}-{money(r['salary_max'])} | [{r['title'][:60]}]({r['url']}) | {r['company'][:35].title()} | {r['min_years_exp']} | {r['posting_date']} |\n" for r in top]
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    recent = [r for r in rows if r["posting_date"] >= cutoff]
    return (f"# Singapore salary benchmarks (MyCareersFuture)\n\nGenerated {today} from `mcf_jobs.csv`: every posting with salary max >= SGD {FLOOR:,}/month since "
            f"{min(r['posting_date'] for r in rows) if rows else '?'}. "
            f"Figures are SGD per month, base only. Yrs = employer-stated minimum years of experience. "
            f"Ranges are what employers publish on the site, not survey estimates.\n\n"
            + table_for(recent, "Last 90 days") + "".join(lvl) + "\n" + "".join(toptab) + "\n" + table_for(rows, "All time"))

# ---------------------------------------------------------------- main
def main():
    global CSV_PATH, STATE, DIGEST, RADAR_NOTE, BENCH_NOTE
    ap = argparse.ArgumentParser(description="Unofficial MyCareersFuture job radar")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--max-details", type=int, default=800)
    ap.add_argument("--since-days", type=int, default=0, help="with --report: treat postings from the last N days as new")
    ap.add_argument("--out", default="data", help="output directory (default ./data)")
    ap.add_argument("--profile", default=None, help="profile JSON (default profile.json, else profile.example.json)")
    a = ap.parse_args()
    prof = Path(a.profile) if a.profile else next((p for p in (Path("profile.json"), HERE / "profile.json", HERE / "profile.example.json") if p.exists()), None)
    if not prof or not prof.exists(): sys.exit("profile file not found")
    load_profile(prof)
    out = Path(a.out).expanduser(); out.mkdir(parents=True, exist_ok=True)
    CSV_PATH, STATE = out / "mcf_jobs.csv", out / "state.json"
    RADAR_NOTE, DIGEST, BENCH_NOTE = out / "radar.md", out / "digest.txt", out / "salary_benchmarks.md"
    lock = out / "run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError:
        print(f"another run holds {lock}; exiting", file=sys.stderr); sys.exit(0)
    import atexit; atexit.register(lambda: lock.unlink(missing_ok=True))
    today = date.today().isoformat()
    rows, state = load_rows(), load_state()
    new = []
    if not a.report:
        since = state.get("last_posting_date", (date.today() - timedelta(days=3)).isoformat())
        since = (datetime.fromisoformat(since) - timedelta(days=1)).date().isoformat()  # 1-day overlap for safety
        print(f"[{today}] profile={prof} collect since {since} backfill={a.backfill}", flush=True)
        new = collect(rows, a.backfill, since)
        rows = new + rows
        save_rows(rows)
        print(f"  new observations: {len(new)}", flush=True)
        n, left = fetch_details(rows, 0 if a.backfill else a.max_details)
        print(f"  details fetched: {n}, still pending: {left}", flush=True)
        new_ids = {id(r) for r in new}
        for r in rows:
            if r["relevance"] == "" or id(r) in new_ids: r["relevance"] = relevance(r)
        new_relevant = [r for r in new if int(r["relevance"]) >= RELEVANT_THRESHOLD]
        save_rows(rows)
        if rows: state["last_posting_date"] = max(r["posting_date"] for r in rows)
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        state["last_new"] = len(new); state["last_new_relevant"] = len(new_relevant)
        save_state(state)
        print(f"  relevant new: {len(new_relevant)}", flush=True)
    else:
        for r in rows:
            r["role_family"] = role_family(r["title"]); r["relevance"] = relevance(r)
        save_rows(rows)
        if a.since_days:
            cut = (date.today() - timedelta(days=a.since_days)).isoformat()
            new = [r for r in rows if r["posting_date"] >= cut]
    RADAR_NOTE.write_text(render_radar(rows, new, today), encoding="utf-8")
    DIGEST.write_text(render_digest(rows, new, today), encoding="utf-8")
    BENCH_NOTE.write_text(render_bench(rows, today), encoding="utf-8")
    print(f"  reports written to {out}", flush=True)

if __name__ == "__main__":
    main()
