"""Shared helpers: HTTP with retries, slugs, org-name normalisation, status log."""
import json, os, re, time, unicodedata, datetime as dt
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
TODAY = dt.date.today()
UA = {"User-Agent": "elimnion-data/1.0 (research pipeline; github.com/Pana-TsK/elimnion-data)"}
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY", "")

_session = requests.Session()
_session.headers.update(UA)
STATUS = {"started": dt.datetime.utcnow().isoformat() + "Z", "calls": {}, "errors": []}


def get(url, params=None, method="GET", body=None, tries=4, pause=0.25):
    """GET/POST JSON with retry + backoff. Returns parsed JSON or None (error logged)."""
    host = re.sub(r"^https?://([^/]+).*", r"\1", url)
    for i in range(tries):
        try:
            if method == "POST":
                r = _session.post(url, json=body, params=params, timeout=60)
            else:
                r = _session.get(url, params=params, timeout=60)
            STATUS["calls"][host] = STATUS["calls"].get(host, 0) + 1
            if r.status_code == 200:
                time.sleep(pause)
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** i * 2)
                continue
            STATUS["errors"].append(f"{host} {r.status_code} {r.url[:160]} :: {r.text[:200]}")
            return None
        except Exception as e:  # network hiccup
            time.sleep(2 ** i * 2)
            last = str(e)
    STATUS["errors"].append(f"{host} failed after {tries}: {url[:200]}")
    return None


def slug(s, maxlen=60):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:maxlen].strip("-")


LEGAL = r"\b(inc|incorporated|ltd|limited|llc|gmbh|sa|sas|nv|bv|ag|ab|plc|corp|corporation|co|srl|spa|as|oy|kk|pty)\b\.?"


def norm_org(name):
    n = (name or "").replace(",", " ")
    n = re.sub(LEGAL, " ", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" .-")
    return n


def org_key(name, engine_index):
    """org:<engine id> when the name matches an engine org, else org:<slug>."""
    n = norm_org(name)
    s = slug(n)
    for eid, ename in engine_index.items():
        es = slug(norm_org(ename))
        if s and (s == es or s == eid or (len(es) > 5 and es in s and len(s) - len(es) < 12)):
            return "org:" + eid
    return "org:" + s


BIG_PHARMA = [
    "pfizer", "roche", "genentech", "hoffmann", "novartis", "merck", "msd", "glaxo", "gsk",
    "sanofi", "astrazeneca", "johnson", "janssen", "abbvie", "bristol", "lilly", "amgen",
    "gilead", "bayer", "novo nordisk", "boehringer", "takeda", "astellas", "daiichi", "biogen",
    "regeneron", "vertex", "moderna", "csl", "otsuka", "teva", "eisai", "viatris", "chugai",
]


def is_big_pharma(name):
    n = (name or "").lower()
    return any(b in n for b in BIG_PHARMA)


def months_ago(m):
    y, mo = TODAY.year, TODAY.month - m
    while mo <= 0:
        mo += 12
        y -= 1
    return dt.date(y, mo, min(TODAY.day, 28)).isoformat()


def write_json(path, obj):
    full = os.path.join(DATA, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0, sort_keys=False)
    return full


def save_status(name, extra=None):
    STATUS["finished"] = dt.datetime.utcnow().isoformat() + "Z"
    if extra:
        STATUS.update(extra)
    write_json(f"_status/{name}.json", STATUS)
