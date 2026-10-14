"""Gap sweep: find drug assets stuck between early clinical data and the next trial.

Deterministic filters over ClinicalTrials.gov, so the Lead Prospector agent only spends
judgment on a short, pre-scored list (data/leads/candidates.json).

Categories
  A  Phase 1 / 1-2 completed in the last 24 months; no later Phase 2+ trial of the same drug
  B  Phase 2 / 2-3 stopped in the last 30 months for funding, business or strategic reasons
  C  Phase 2 not yet recruiting although the planned start passed 6-36 months ago
  D  Phase 2 completed in the last 30 months with results posted; no Phase 3 / new Phase 2 since

Sponsor size is estimated from the data itself: an industry sponsor with many trials in the
pulled sets is treated as large (it has a BD team) and dropped.
"""
import re
from collections import Counter, defaultdict
from common import TODAY, months_ago, is_big_pharma, write_json, save_status, STATUS
from sources import ct_search, ct_url

GENERIC = re.compile(r"\b(placebo|saline|vehicle|sham|standard of care|usual care|best supportive care|control|"
                     r"no intervention|observation|matching|dummy|sugar pill|chemotherapy|radiotherapy)\b", re.I)
PK_STUDY = re.compile(r"bioequivalen|food effect|drug[- ]drug interaction|relative bioavailab|"
                      r"pharmacokinetic comparison|biosimilar|renal impairment|hepatic impairment|"
                      r"thorough qt|mass balance|\bADME\b|healthy (chinese|japanese) ", re.I)
CODE = re.compile(r"\b[A-Za-z]{1,6}[- ]?\d{2,7}[A-Za-z]?\b")
# Strong business reasons; a bare "sponsor decision" is ambiguous and scores lower.
BUSINESS = re.compile(r"fund|financ|budget|business|strateg|portfolio|priorit|acqui|merger|commercial|"
                      r"bankrupt|insolven|economic|reorgani|resource|partner|licens|company decision", re.I)
VAGUE = re.compile(r"sponsor('s)?\s+decision|sponsor request|administrative", re.I)
CLINICAL_STOP = re.compile(r"safety|efficacy|futility|adverse|toxicit|lack of|dsmb|data monitoring|"
                           r"serious|death|benefit[- ]risk|enrol|recruit|accrual|endpoint|interim|"
                           r"transitioned to commercial|approved|marketed", re.I)
EUROPE = {"Belgium", "Netherlands", "Germany", "France", "Spain", "Italy", "Denmark", "Sweden", "Finland",
          "Norway", "Austria", "Switzerland", "United Kingdom", "Ireland", "Poland", "Czechia", "Portugal"}
# Large companies and pharma subsidiaries that have BD teams of their own.
LARGE = re.compile(r"alexion|viiv|genentech|chugai|kite|juno|celgene|loxo|acceleron|biontech|incyte|genmab|"
                   r"servier|seagen|ipsen|\bucb\b|lundbeck|jazz|beigene|argenx|sumitomo|kyowa|ono pharm|"
                   r"shionogi|hengrui|hansoh|innovent|zai lab|sinopharm|qilu|cspc|simcere|grifols|"
                   r"menarini|chiesi|pierre fabre|almirall|ferring|leo pharma|novo|organon|sandoz|"
                   r"alnylam|ionis|biomarin|sarepta|exelixis|neurocrine|united therapeutics|bayer|"
                   r"astellas|eisai|otsuka|takeda|daiichi|mitsubishi tanabe", re.I)


def drug_names(study):
    """Normalised drug names of a study; code-like names (e.g. abc-123) first."""
    names = []
    for i in study["interventions"]:
        if i["type"] not in ("DRUG", "BIOLOGICAL", "GENETIC", "COMBINATION_PRODUCT"):
            continue
        for raw in [i["name"]] + list(i.get("other") or []):
            raw = re.sub(r"^(drug|biological|genetic)\s*:\s*", "", raw or "", flags=re.I)
            raw = re.split(r"\s+(is|are|was|for|in|at)\s+", raw)[0]
            for part in re.split(r"\s*(?:\+|/|,|;| and | plus | with )\s*", raw):
                n = re.sub(r"\(.*?\)|[®™]|\d+(\.\d+)?\s*(mg|µg|ug|mcg|g|ml|iu|%|mg/kg)\b.*", "", part, flags=re.I)
                n = re.sub(r"\s+", " ", n).strip(" -.").lower()
                if 3 <= len(n) <= 40 and not GENERIC.search(n) and n not in names:
                    names.append(n)
    return sorted(names, key=lambda n: (not CODE.search(n), names.index(n)))


def phase_rank(phases):
    r = {"EARLY_PHASE1": 0.5, "PHASE1": 1, "PHASE2": 2, "PHASE3": 3, "PHASE4": 4}
    return max([r.get(p, 0) for p in phases] or [0])


def partnered(s):
    return any(c["class"] == "INDUSTRY" for c in s["collaborators"])


def main():
    since24, since30, since36 = months_ago(24), months_ago(30), months_ago(36)
    today = TODAY.isoformat()
    drug_q = "AREA[InterventionType](DRUG OR BIOLOGICAL OR GENETIC)"

    # 1. Follow-on index: every Phase 2+ trial started in the last 36 months (any status).
    later = ct_search(max_records=40000,
                      filter_advanced=f"(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) AND AREA[StartDate]RANGE[{since36},MAX] AND {drug_q}")
    a = ct_search(max_records=20000, filter_overallStatus="COMPLETED",
                  filter_advanced=f"(AREA[Phase]PHASE1 OR AREA[Phase]EARLY_PHASE1) AND AREA[CompletionDate]RANGE[{since24},{today}] AND {drug_q}")
    b = ct_search(max_records=10000, filter_overallStatus="TERMINATED,WITHDRAWN,SUSPENDED",
                  filter_advanced=f"AREA[Phase]PHASE2 AND AREA[LastUpdatePostDate]RANGE[{since30},MAX] AND {drug_q}")
    c = ct_search(max_records=5000, filter_overallStatus="NOT_YET_RECRUITING",
                  filter_advanced=f"AREA[Phase]PHASE2 AND AREA[StartDate]RANGE[{since36},{months_ago(6)}] AND {drug_q}")
    d = ct_search(max_records=10000, filter_overallStatus="COMPLETED",
                  filter_advanced=f"AREA[Phase]PHASE2 AND AREA[CompletionDate]RANGE[{since30},{today}] AND {drug_q}")

    # Sponsor size proxy: number of distinct trials per industry sponsor across everything pulled.
    trials_per_sponsor = Counter()
    for s in {x["nct"]: x for x in later + a + b + c + d}.values():
        if s["sponsorClass"] == "INDUSTRY":
            trials_per_sponsor[s["sponsor"]] += 1

    def is_large(s):
        n = s["sponsor"]
        return (is_big_pharma(n) or LARGE.search(n) or s["sponsorClass"] in ("NIH", "FED")
                or (s["sponsorClass"] == "INDUSTRY" and trials_per_sponsor[n] >= 15))

    follow = defaultdict(list)
    for s in later:
        for n in drug_names(s):
            follow[n].append(s)

    def follow_on(s, min_rank):
        """NCT of a later trial of the same drug at >= min_rank, same sponsor or overlapping condition."""
        conds = {x.lower() for x in s["conditions"]}
        for n in drug_names(s)[:3]:
            for t in follow.get(n, []):
                if t["nct"] == s["nct"] or phase_rank(t["phases"]) < min_rank or (t["start"] or "") < (s["start"] or ""):
                    continue
                if t["sponsor"] == s["sponsor"] or conds & {x.lower() for x in t["conditions"]}:
                    return t["nct"]
        return None

    cands = []

    def add(s, cat, why, bonus=0):
        names = drug_names(s)
        if not names or PK_STUDY.search(s["title"]) or is_large(s) or partnered(s):
            return
        countries = {x["country"] for x in s["sites"]}
        code_like = bool(CODE.search(names[0]))
        academic = s["sponsorClass"] in ("OTHER", "NETWORK", "OTHER_GOV", "INDIV")
        score = bonus
        score += 3 if academic else 2                      # nobody else is brokering these
        score += 2 if s["hasResults"] else 0               # data in hand
        score += 2 if code_like else 0                     # a proprietary asset, not a generic
        score += 2 if (s["completion"] or s["updated"] or "") >= months_ago(12) else 0   # fresh
        score += 1 if (s["enrollment"] or 0) >= 20 else 0
        score += 1 if countries & EUROPE else 0
        cands.append({
            "cat": cat, "why": why, "preScore": score, "nct": s["nct"], "url": ct_url(s["nct"]),
            "title": s["title"], "drug": names[0], "drugs": names[:4], "codeLike": code_like,
            "phases": s["phases"], "status": s["status"], "whyStopped": s["whyStopped"], "start": s["start"],
            "completion": s["completion"], "enrollment": s["enrollment"], "hasResults": s["hasResults"],
            "sponsor": s["sponsor"], "sponsorClass": s["sponsorClass"], "sponsorTrials": trials_per_sponsor[s["sponsor"]],
            "conditions": s["conditions"][:5], "countries": sorted(countries)[:10],
            "officials": [o["name"] + (" (" + o["affiliation"] + ")" if o["affiliation"] else "") for o in s["officials"][:3]],
        })

    for s in a:
        if phase_rank(s["phases"]) <= 2 and not follow_on(s, 2):
            add(s, "A", f"Phase 1 completed {s['completion']}; no Phase 2 of the same drug since")
    for s in b:
        w = s["whyStopped"] or ""
        stopped = s["completion"] or s["primaryCompletion"] or s["updated"] or ""
        if stopped < since30 or CLINICAL_STOP.search(w) or follow_on(s, 2):
            continue
        if BUSINESS.search(w):
            add(s, "B", f"{s['status'].title()} {stopped}: '{w[:160]}'", bonus=3)
        elif VAGUE.search(w):
            add(s, "B", f"{s['status'].title()} {stopped}: '{w[:160]}' (reason vague; check)", bonus=1)
    for s in c:
        add(s, "C", f"Phase 2 planned to start {s['start']} but still not recruiting", bonus=-1)
    for s in d:
        if s["hasResults"] and phase_rank(s["phases"]) < 3 and not follow_on(s, 3):
            add(s, "D", f"Phase 2 completed {s['completion']} with posted results; no Phase 3 / new Phase 2 since", bonus=1)

    # One row per (sponsor, lead drug), best score kept; then cap per category so no bucket floods the list.
    best = {}
    for x in cands:
        k = (x["sponsor"].lower(), x["drug"])
        if k not in best or x["preScore"] > best[k]["preScore"]:
            best[k] = x
    rows = sorted(best.values(), key=lambda x: (-x["preScore"], -(int((x["completion"] or "0")[:4] or 0))))
    caps, kept, n = {"A": 600, "B": 400, "C": 200, "D": 400}, [], Counter()
    for r in rows:
        if n[r["cat"]] < caps[r["cat"]]:
            kept.append(r)
            n[r["cat"]] += 1
    write_json("leads/candidates.json", {"generated": TODAY.isoformat(), "count": len(kept), "byCategory": dict(n),
                                         "scanned": {"A": len(a), "B": len(b), "C": len(c), "D": len(d), "followIndex": len(later)},
                                         "rows": kept})
    save_status("sweep", {"candidates": len(kept), "byCategory": dict(n)})
    print("candidates", len(kept), dict(n), STATUS["calls"], STATUS["errors"][:3])


if __name__ == "__main__":
    main()
