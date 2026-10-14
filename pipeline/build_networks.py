"""Deep network per watchlist asset, in the Deal Engine 'networks' schema.

Output: data/networks/<assetId>.json  {assetId, updated, sources, nodes:[{k,t,n,sub,url,c}], edges:[{a,b,r}]}
        data/networks/_hubs.json      people/orgs that appear in two or more assets' networks
The Network Builder agent copies these into the database and adds judgment (links, notes).
"""
import json, os, re, sys
from collections import defaultdict
from common import TODAY, slug, org_key, write_json, save_status, STATUS, ROOT, DATA
from sources import ct_search, ct_url, oa_works, nih_projects

MAX_NODES, MAX_EDGES = 700, 1600
HOSP = re.compile(r"hospital|clinic|medical cent|health|ziekenhuis|\buz\b|hôpital|hopital|klinik|chu\b|ospedale|"
                  r"universitair|children's|childrens", re.I)
UNI = re.compile(r"universit|college|institut|school|faculty|kuleuven|ku leuven|academ", re.I)
FOUND = re.compile(r"foundation|fondation|stichting|association|society|trust|alliance|charity", re.I)


def org_type(name, cls=""):
    if cls in ("NIH", "FED", "OTHER_GOV"):
        return "funder"
    if cls == "INDUSTRY":
        return "company"
    if HOSP.search(name or ""):
        return "hospital"
    if FOUND.search(name or ""):
        return "foundation"
    if UNI.search(name or "") or cls in ("OTHER", "NETWORK"):
        return "academic"
    return "company"


OA_TYPE = {"education": "academic", "healthcare": "hospital", "company": "company", "nonprofit": "foundation",
           "government": "funder", "funder": "funder", "facility": "academic", "archive": "academic", "other": "academic"}


PLACEHOLDER = re.compile(r"\d|@|\b(inc|llc|ltd|gmbh|pharma\w*|therapeutics|bio\w*|clinical|trials?|development|"
                         r"medical|director|cares|management|leader|expert|research|call|site|team|study|sponsor|"
                         r"department|center|centre|hospital|university|information|contact|program|group)\b", re.I)
FAKE_SITE = re.compile(r"^(research|investigational|clinical|study|local|trial)\s+(site|institution|center)|^site\b|"
                       r"\bsite\s*(no\.?|number|#)?\s*\d", re.I)


def is_real_person(name):
    words = [w for w in re.split(r"\s+", name) if w]
    return 2 <= len(words) <= 6 and len(name) <= 60 and not PLACEHOLDER.search(name)


def clean_drug(name):
    n = re.sub(r"\(.*?\)|[®™]", " ", name or "")
    n = re.sub(r"[-–]?\s*(starting\s+)?dose\b.*|\b\d+(\.\d+)?\s*(mg|µg|ug|mcg|g|ml|mg/kg)\b.*|\b(hydrochloride|hcl|sodium|"
               r"oral solution|tablets?|capsules?|injection|transdermal delivery system)\b", " ", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip(" -,:;")


class Graph:
    def __init__(self, engine_orgs):
        self.nodes, self.edges, self.engine = {}, set(), engine_orgs

    def node(self, k, t, n, sub="", url="", c=""):
        if not k or k.endswith(":"):
            return None
        cur = self.nodes.get(k)
        if cur:
            for f, v in (("sub", sub), ("url", url), ("c", c)):
                if v and not cur.get(f):
                    cur[f] = v[:90]
        else:
            self.nodes[k] = {"k": k, "t": t, "n": (n or k)[:90], "sub": (sub or "")[:90], "url": url or "", "c": c or ""}
        return k

    def edge(self, a, b, r):
        if a and b and a != b:
            self.edges.add((a, b, r))

    def org(self, name, cls="", c="", t=None):
        if not name:
            return None
        k = org_key(name, self.engine)
        eid = k[4:]
        disp = self.engine.get(eid, name)
        return self.node(k, t or org_type(name, cls), disp, "", "", c)

    def person(self, name, sub="", c=""):
        name = re.sub(r",?\s*(MD|PhD|M\.D\.|Ph\.D\.|Prof\.?|Dr\.?|MSc|MBBS|PharmD|FRCP\w*)\b\.?", "", name or "")
        name = re.sub(r"\s+", " ", name).strip(" ,.")
        if not is_real_person(name):
            return None
        return self.node("person:" + slug(name), "person", name, sub, "", c)

    def prune(self, keep):
        deg = defaultdict(int)
        for a, b, _ in self.edges:
            deg[a] += 1
            deg[b] += 1
        order = sorted((k for k in self.nodes if k not in keep), key=lambda k: (deg[k], self.nodes[k]["t"] not in ("person", "paper")))
        while len(self.nodes) > MAX_NODES and order:
            self.nodes.pop(order.pop(0), None)
        self.edges = {e for e in self.edges if e[0] in self.nodes and e[1] in self.nodes}
        if len(self.edges) > MAX_EDGES:
            ranked = sorted(self.edges, key=lambda e: (e[2] == "site_of", -(deg[e[0]] + deg[e[1]])))
            self.edges = set(ranked[:MAX_EDGES])

    def dump(self, asset_id, sources):
        return {"assetId": asset_id, "updated": TODAY.isoformat(), "generatedBy": "elimnion-data pipeline",
                "sources": sources, "nodes": list(self.nodes.values()),
                "edges": [{"a": a, "b": b, "r": r} for a, b, r in sorted(self.edges)]}


def add_trial(g, s, link_from=None, rel="tested_in", sites=25):
    yrs = "-".join(x[:4] for x in (s["start"], s["completion"]) if x)
    ph = "/".join(p.replace("PHASE", "P").replace("EARLY_", "e") for p in s["phases"]) or "NA"
    tk = g.node("trial:" + s["nct"].lower(), "trial", s["title"], f"{ph} · {s['status'].lower()} · {yrs}", ct_url(s["nct"]))
    if link_from:
        g.edge(link_from, tk, rel)
    sp = g.org(s["sponsor"], s["sponsorClass"])
    g.edge(sp, tk, "sponsors")
    for c in s["collaborators"]:
        g.edge(g.org(c["name"], c["class"]), tk, "collaborates")
    for o in s["officials"]:
        aff = g.org(o["affiliation"]) if o["affiliation"] else None
        p = g.person(o["name"], f"{(o['role'] or '').replace('_', ' ').lower()} · {o['affiliation']}"[:90])
        g.edge(p, tk, "investigator")
        g.edge(p, aff, "affiliated")
    for loc in s["sites"][:sites]:
        if loc["facility"] and not FAKE_SITE.search(loc["facility"]):
            g.edge(g.org(loc["facility"], c=loc["country"]), tk, "site_of")
    return tk, sp


def add_paper(g, w, asset_key=None):
    if w["pmid"]:
        k, url = "paper:pmid-" + w["pmid"], f"https://pubmed.ncbi.nlm.nih.gov/{w['pmid']}"
    elif w["doi"]:
        k, url = "paper:doi-" + slug(w["doi"], 80), "https://doi.org/" + w["doi"]
    else:
        k, url = "paper:oa-" + w["id"].lower(), "https://openalex.org/" + w["id"]
    pk = g.node(k, "paper", w["title"], f"{w['journal']} {w['year']} · cited {w['cited']}", url)
    if asset_key:
        g.edge(pk, asset_key, "cites_asset")
    for a in w["authors"]:
        if not (a["pos"] in ("first", "last") or a["corr"]):
            continue
        inst = a["inst"][0] if a["inst"] else None
        p = g.person(a["name"], (inst or {}).get("name", ""), (inst or {}).get("country", ""))
        if p and a["orcid"] and not g.nodes[p]["url"]:
            g.nodes[p]["url"] = "https://orcid.org/" + a["orcid"]
        g.edge(p, pk, "authored")
        for i in a["inst"][:2]:
            g.edge(p, g.org(i["name"], c=i["country"], t=OA_TYPE.get(i["type"], "academic")), "affiliated")
    for gr in w["grants"][:4]:
        if gr["funder"]:
            g.edge(g.org(gr["funder"], t="funder"), pk, "funded")
    return pk


def build(asset, engine_orgs):
    g = Graph(engine_orgs)
    ak = g.node("asset:" + asset["id"], "asset", asset["name"], "; ".join(asset["conditions"])[:90])
    src = defaultdict(int)
    if asset.get("owner") and asset["owner"] in engine_orgs:
        g.edge(g.node("org:" + asset["owner"], "academic" if "leuven" in asset["owner"] else "company",
                      engine_orgs[asset["owner"]]), ak, "owns")
    own = {d.lower() for d in asset["drugs"]}

    # 1. Trials of the asset itself
    seen = set()
    for d in asset["drugs"]:
        for s in ct_search(max_records=150, query_intr=d):
            if s["nct"] in seen:
                continue
            seen.add(s["nct"])
            add_trial(g, s, ak, "tested_in")
            src["trials"] += 1
    # 2. Indication trials (competitors, sites, investigators): Phase 2/3 started since 2019
    for cond in asset["conditions"][:2]:
        for s in ct_search(max_records=40, query_cond=cond, sort="LastUpdatePostDate:desc",
                           filter_advanced="(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) AND AREA[StartDate]RANGE[2019-01-01,MAX]"):
            if s["nct"] in seen:
                continue
            seen.add(s["nct"])
            tk, sp = add_trial(g, s, None, sites=10)
            src["trials"] += 1
            for i in s["interventions"]:
                if i["type"] not in ("DRUG", "BIOLOGICAL", "GENETIC") or re.search(r"placebo|standard|vehicle", i["name"], re.I):
                    continue
                dn = clean_drug(i["name"])
                if len(dn) < 3 or dn.lower() in own:
                    continue
                dk = g.node("drug:" + slug(dn), "drug", dn, s["sponsor"])
                g.edge(dk, tk, "tested_in")
                g.edge(sp, dk, "develops")
                g.edge(dk, ak, "competes")
    # 3. Publications (OpenAlex)
    cond0 = asset["conditions"][0] if asset["conditions"] else ""
    for d in asset["drugs"]:
        for w in oa_works(f"{d} {cond0}".strip(), n=50):
            add_paper(g, w, ak)
            src["papers"] += 1
        for w in oa_works(d, n=25, sort="cited_by_count:desc"):
            add_paper(g, w, ak)
            src["papers"] += 1
    for cond in asset["conditions"][:2]:
        for w in oa_works(cond, n=35, since=TODAY.year - 6, sort="cited_by_count:desc"):
            add_paper(g, w)
            src["papers"] += 1
    # 4. Grants (NIH RePORTER)
    for term in (asset["drugs"][:1] or asset["conditions"][:1]):
        for p in nih_projects(f'"{term}"', n=20):
            gk = g.node("grant:nih-" + slug(p["num"]), "grant", p["title"],
                        f"NIH {p['ic']} · ${(p['amount'] or 0):,} · {p['start'][:4]}-{p['end'][:4]}",
                        f"https://reporter.nih.gov/project-details/{p['appl']}")
            g.edge(g.org("NIH " + p["ic"], "NIH", "US", t="funder"), gk, "funded")
            org = g.org(p["org"], c=p["country"][:2] if p["country"] else "")
            for pi in p["pis"][:2]:
                pk = g.person(pi.title(), p["org"])
                g.edge(pk, gk, "grant_pi")
                g.edge(pk, org, "affiliated")
            src["grants"] += 1
    g.prune(keep={ak})
    return g.dump(asset["id"], dict(src))


def lead_assets(n=25):
    """Top gap-sweep candidates as pseudo-assets, so new leads have a map before anyone asks.
    The agents match them by NCT id (file data/networks/nct-<nct>.json)."""
    path = os.path.join(DATA, "leads", "candidates.json")
    if not os.path.exists(path):
        return []
    rows = json.load(open(path))["rows"]
    rows = sorted([r for r in rows if r["cat"] in ("A", "B", "D") and r["codeLike"]], key=lambda r: -r["preScore"])[:n]
    return [{"id": "nct-" + r["nct"].lower(), "name": r["drug"].upper(), "drugs": [r["drug"]],
             "conditions": [c for c in r["conditions"][:2]], "target": "", "owner": ""} for r in rows]


def main(only=None):
    wl = json.load(open(os.path.join(ROOT, "watchlist.json")))
    engine = wl.get("orgs", {})
    built = {}
    assets = wl["assets"] + ([] if only else lead_assets())
    for a in assets:
        if only and a["id"] not in only:
            continue
        doc = build(a, engine)
        size = len(json.dumps(doc))
        write_json(f"networks/{a['id']}.json", doc)
        built[a["id"]] = {"nodes": len(doc["nodes"]), "edges": len(doc["edges"]), "bytes": size}
        print(a["id"], built[a["id"]], flush=True)
    # hubs across all asset networks on disk
    seen = defaultdict(set)
    meta = {}
    netdir = os.path.join(DATA, "networks")
    for f in os.listdir(netdir):
        if f.startswith("_"):
            continue
        doc = json.load(open(os.path.join(netdir, f)))
        for n in doc["nodes"]:
            if n["t"] in ("person", "academic", "hospital", "company", "foundation", "investor"):
                seen[n["k"]].add(doc["assetId"])
                meta[n["k"]] = n
    hubs = sorted(([meta[k] | {"assets": sorted(v)} for k, v in seen.items()
                   if len(v) >= 2 and not FAKE_SITE.search(meta[k]["n"])]),
                  key=lambda x: (x["t"] != "person", -len(x["assets"])))
    write_json("networks/_hubs.json", {"generated": TODAY.isoformat(), "hubs": hubs[:500]})
    save_status("networks", {"built": built, "hubs": len(hubs)})


if __name__ == "__main__":
    main(set(sys.argv[1:]) or None)
