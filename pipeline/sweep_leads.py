"""Gap sweep: find drug assets stuck between early clinical data and the next trial.

Runs deterministic filters over ClinicalTrials.gov so the Lead Prospector agent only
spends judgment on a short, pre-scored list (data/leads/candidates.json).

Categories
  A  Phase 1 / 1-2 completed in the last 24 months, no later Phase 2+ trial of the same drug
  B  Phase 2 / 2-3 terminated, withdrawn or suspended in the last 24 months for business/funding reasons
  C  Phase 2 not yet recruiting although the planned start is > 6 months ago
  D  Phase 2 completed in the last 30 months with results posted and no follow-on trial
"""
import re
from collections import defaultdict
from common import TODAY, months_ago, is_big_pharma, write_json, save_status, STATUS
from sources import ct_search, ct_url

GENERIC = re.compile(r"\b(placebo|saline|vehicle|sham|standard of care|usual care|best supportive care|control|"
                     r"no intervention|observation|matching|dummy|sugar pill)\b", re.I)
PK_STUDY = re.compile(r"bioequivalen|food effect|drug[- ]drug interaction|relative bioavailab|"
                      r"pharmacokinetic comparison|biosimilar|renal impairment|hepatic impairment|"
                      r"thorough qt|mass balance|\bADME\b", re.I)
CODE = re.compile(r"\b[A-Z]{1,6}[- ]?\d{2,7}[A-Z]?\b")
BUSINESS = re.compile(r"fund|financ|business|sponsor('s)? decision|company decision|strateg|portfolio|priorit|"
                      r"resource|budget|acqui|commercial|partner|merger|bankrupt|insolven|economic|reorgani", re.I)
CLINICAL_STOP = re.compile(r"safety|efficacy|futility|adverse|toxicit|lack of (effect|benefit)|dsmb|"
                           r"data monitoring|serious|death|benefit[- ]risk", re.I)
EUROPE = {"Belgium", "Netherlands", "Germany", "France", "Spain", "Italy", "Denmark", "Sweden", "Finland",
          "Norway", "Austria", "Switzerland", "United Kingdom", "Ireland", "Poland", "Czechia", "Portugal"}


def drug_names(study):
    names = set()
    for i in study["interventions"]:
        if i["type"] not in ("DRUG", "BIOLOGICAL", "GENETIC", "COMBINATION_PRODUCT"):
            continue
        for raw in [i["name"]] + list(i.get("other") or []):
            for part in re.split(r"\s*(?:\+|/|,| and | plus | with )\s*", raw or ""):
                n = re.sub(r"\(.*?\)|\d+(\.\d+)?\s*(mg|µg|ug|mcg|g|ml|iu|%|mg/kg)\b.*", "", part, flags=re.I)
                n = re.sub(r"\s+", " ", n).strip(" -.").lower()
                if len(n) >= 3 and not GENERIC.search(n):
                    names.add(n)
    return names


def phase_rank(phases):
    r = {"EARLY_PHASE1": 0.5, "PHASE1": 1, "PHASE2": 2, "PHASE3": 3, "PHASE4": 4}
    return max([r.get(p, 0) for p in phases] or [0])


def small_sponsor(s):
    return not is_big_pharma(s["sponsor"]) and s["sponsorClass"] not in ("NIH", "FED")


def partnered(s):
    return any(c["class"] == "INDUSTRY" for c in s["collaborators"])


def main():
    since24, since30, since36 = months_ago(24), months_ago(30), months_ago(36)
    today = TODAY.isoformat()
    drug_q = "AREA[InterventionType](DRUG OR BIOLOGICAL OR GENETIC)"

    # 1. Follow-on index: every Phase 2+ trial started in the last 36 months (any status).
    later = ct_search(max_records=40000,
                      filter_advanced=f"(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) AND AREA[StartDate]RANGE[{since36},MAX] AND {drug_q}")
    follow = defaultdict(list)
    for s in later:
        for n in drug_names(s):
            follow[n].append(s)

    def follow_on(s, min_rank):
        """Later trial of the same drug at >= min_rank, by the same sponsor or in an overlapping condition."""
        conds = {c.lower() for c in s["conditions"]}
        for n in drug_names(s):
            for t in follow.get(n, []):
                if t["nct"] == s["nct"] or phase_rank(t["phases"]) < min_rank:
                    continue
                if (t["start"] or "") < (s["start"] or ""):
                    continue
                if t["sponsor"] == s["sponsor"] or conds & {c.lower() for c in t["conditions"]}:
                    return t["nct"]
        return None

    cands = []

    def add(s, cat, why):
        names = sorted(drug_names(s))
        if not names or PK_STUDY.search(s["title"]) or not small_sponsor(s):
            return
        countries = {x["country"] for x in s["sites"]}
        code_like = bool(CODE.search(" ".join(i["name"] for i in s["interventions"])))
        score = 0
        score += 3 if s["sponsorClass"] in ("OTHER", "NETWORK", "OTHER_GOV") and not partnered(s) else 0
        score += 2 if s["sponsorClass"] == "INDUSTRY" and not partnered(s) else 0
        score += 2 if s["hasResults"] else 0
        score += 2 if code_like else 0
        score += 2 if (s["completion"] or "") >= months_ago(12) else 0
        score += 1 if (s["enrollment"] or 0) >= 20 else 0
        score += 1 if countries & EUROPE else 0
        score += 3 if cat == "B" else 0
        cands.append({
            "cat": cat, "why": why, "preScore": score, "nct": s["nct"], "url": ct_url(s["nct"]),
            "title": s["title"], "drugs": names, "codeLike": code_like, "phases": s["phases"],
            "status": s["status"], "whyStopped": s["whyStopped"], "start": s["start"],
            "completion": s["completion"], "enrollment": s["enrollment"], "hasResults": s["hasResults"],
            "sponsor": s["sponsor"], "sponsorClass": s["sponsorClass"],
            "collaborators": [c["name"] for c in s["collaborators"]], "industryPartner": partnered(s),
            "conditions": s["conditions"][:6], "countries": sorted(countries)[:12],
            "officials": [o["name"] + (" (" + o["affiliation"] + ")" if o["affiliation"] else "") for o in s["officials"][:3]],
        })

    # A. Phase 1 / 1-2 completed in the last 24 months, no later Phase 2+.
    a = ct_search(max_records=20000, filter_overallStatus="COMPLETED",
                  filter_advanced=f"(AREA[Phase]PHASE1 OR AREA[Phase]EARLY_PHASE1) AND AREA[CompletionDate]RANGE[{since24},{today}] AND {drug_q}")
    for s in a:
        if phase_rank(s["phases"]) <= 2 and not follow_on(s, 2):
            add(s, "A", "Phase 1 completed " + (s["completion"] or "?") + "; no Phase 2 of the same drug registered since")

    # B. Phase 2 / 2-3 stopped for business or funding reasons.
    b = ct_search(max_records=10000, filter_overallStatus="TERMINATED,WITHDRAWN,SUSPENDED",
                  filter_advanced=f"AREA[Phase]PHASE2 AND AREA[LastUpdatePostDate]RANGE[{since24},MAX] AND {drug_q}")
    for s in b:
        w = s["whyStopped"] or ""
        if BUSINESS.search(w) and not CLINICAL_STOP.search(w) and not follow_on(s, 2):
            add(s, "B", f"{s['status'].title()} — '{w[:160]}'")

    # C. Phase 2 not yet recruiting, planned start passed > 6 months ago.
    c = ct_search(max_records=5000, filter_overallStatus="NOT_YET_RECRUITING",
                  filter_advanced=f"AREA[Phase]PHASE2 AND AREA[StartDate]RANGE[MIN,{months_ago(6)}] AND {drug_q}")
    for s in c:
        add(s, "C", f"Phase 2 planned to start {s['start']} but still not recruiting")

    # D. Phase 2 completed with results, no follow-on.
    d = ct_search(max_records=10000, filter_overallStatus="COMPLETED",
                  filter_advanced=f"AREA[Phase]PHASE2 AND AREA[CompletionDate]RANGE[{since30},{today}] AND {drug_q}")
    for s in d:
        if s["hasResults"] and phase_rank(s["phases"]) < 3 and not follow_on(s, 3):
            add(s, "D", "Phase 2 completed " + (s["completion"] or "?") + " with posted results; no Phase 3 / new Phase 2 since")

    # Collapse to one row per (sponsor, main drug); keep the best-scoring.
    best = {}
    for x in cands:
        k = (x["sponsor"].lower(), x["drugs"][0])
        if k not in best or x["preScore"] > best[k]["preScore"]:
            best[k] = x
    rows = sorted(best.values(), key=lambda x: (-x["preScore"], x["completion"] or ""), reverse=False)[:2000]
    write_json("leads/candidates.json", {"generated": TODAY.isoformat(), "count": len(rows),
                                         "scanned": {"A": len(a), "B": len(b), "C": len(c), "D": len(d), "followIndex": len(later)},
                                         "rows": rows})
    save_status("sweep", {"candidates": len(rows)})
    print("candidates", len(rows), STATUS["calls"], STATUS["errors"][:5])


if __name__ == "__main__":
    main()
