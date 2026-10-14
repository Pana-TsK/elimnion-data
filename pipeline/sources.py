"""Thin clients for the free databases. Each returns compact, flat records."""
from common import get, OPENALEX_KEY

# ---------------------------------------------------------------- ClinicalTrials.gov API v2
CT = "https://clinicaltrials.gov/api/v2/studies"
CT_FIELDS = ",".join([
    "NCTId", "BriefTitle", "OverallStatus", "WhyStopped", "Phase", "StudyType",
    "StartDate", "PrimaryCompletionDate", "CompletionDate", "LastUpdatePostDate",
    "LeadSponsorName", "LeadSponsorClass", "CollaboratorName", "CollaboratorClass",
    "OverallOfficialName", "OverallOfficialAffiliation", "OverallOfficialRole",
    "LocationFacility", "LocationCity", "LocationCountry",
    "InterventionName", "InterventionType", "InterventionOtherName",
    "Condition", "EnrollmentCount", "HasResults",
])


def _flat_study(s):
    p = s.get("protocolSection", {})
    idm, st = p.get("identificationModule", {}), p.get("statusModule", {})
    sp, cl = p.get("sponsorCollaboratorsModule", {}), p.get("contactsLocationsModule", {})
    de, ai = p.get("designModule", {}), p.get("armsInterventionsModule", {})
    d = lambda k: (st.get(k) or {}).get("date", "")
    return {
        "nct": idm.get("nctId"), "title": idm.get("briefTitle", ""),
        "status": st.get("overallStatus", ""), "whyStopped": st.get("whyStopped", ""),
        "phases": de.get("phases", []), "type": de.get("studyType", ""),
        "enrollment": (de.get("enrollmentInfo") or {}).get("count"),
        "start": d("startDateStruct"), "primaryCompletion": d("primaryCompletionDateStruct"),
        "completion": d("completionDateStruct"), "updated": d("lastUpdatePostDateStruct"),
        "sponsor": (sp.get("leadSponsor") or {}).get("name", ""),
        "sponsorClass": (sp.get("leadSponsor") or {}).get("class", ""),
        "collaborators": [{"name": c.get("name", ""), "class": c.get("class", "")} for c in sp.get("collaborators", [])],
        "officials": [{"name": o.get("name", ""), "affiliation": o.get("affiliation", ""), "role": o.get("role", "")}
                      for o in cl.get("overallOfficials", [])],
        "sites": [{"facility": l.get("facility", ""), "city": l.get("city", ""), "country": l.get("country", "")}
                  for l in cl.get("locations", [])],
        "interventions": [{"type": i.get("type", ""), "name": i.get("name", ""), "other": i.get("otherNames", [])}
                          for i in ai.get("interventions", [])],
        "conditions": p.get("conditionsModule", {}).get("conditions", []),
        "hasResults": bool(s.get("hasResults")),
    }


def ct_search(max_records=1000, **params):
    """params use API names with dots replaced by underscores, e.g. query_cond=..., filter_advanced=..."""
    q = {k.replace("_", ".", 1) if k.startswith(("query_", "filter_")) else k: v for k, v in params.items()}
    q.update({"fields": CT_FIELDS, "pageSize": min(1000, max_records), "format": "json"})
    out, token = [], None
    while len(out) < max_records:
        if token:
            q["pageToken"] = token
        js = get(CT, q)
        if not js and "fields" in q and not out:
            q.pop("fields")  # a renamed field name makes the API 400: fall back to full records
            js = get(CT, q)
        if not js:
            break
        out += [_flat_study(s) for s in js.get("studies", [])]
        token = js.get("nextPageToken")
        if not token:
            break
    return out[:max_records]


def ct_url(nct):
    return f"https://clinicaltrials.gov/study/{nct}"


# ---------------------------------------------------------------- OpenAlex
OA = "https://api.openalex.org/works"
OA_SELECT = "id,doi,title,publication_year,authorships,grants,primary_location,cited_by_count,ids"


def oa_works(search, n=100, since=2016, sort=None, extra_filter=""):
    params = {"search": search, "per-page": min(200, n), "select": OA_SELECT,
              "filter": f"from_publication_date:{since}-01-01" + (("," + extra_filter) if extra_filter else "")}
    if sort:
        params["sort"] = sort
    if OPENALEX_KEY:
        params["api_key"] = OPENALEX_KEY
    out, cursor = [], "*"
    while len(out) < n and cursor:
        params["cursor"] = cursor
        js = get(OA, params)
        if not js:
            break
        for w in js.get("results", []):
            ids = w.get("ids") or {}
            pmid = (ids.get("pmid") or "").rsplit("/", 1)[-1]
            src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
            auths = []
            for a in w.get("authorships", []):
                au = a.get("author") or {}
                auths.append({
                    "name": au.get("display_name", ""), "orcid": (au.get("orcid") or "").rsplit("/", 1)[-1],
                    "pos": a.get("author_position", ""), "corr": bool(a.get("is_corresponding")),
                    "inst": [{"name": i.get("display_name", ""), "type": i.get("type", ""),
                              "country": i.get("country_code", ""), "ror": (i.get("ror") or "").rsplit("/", 1)[-1]}
                             for i in a.get("institutions", [])],
                })
            out.append({
                "id": w.get("id", "").rsplit("/", 1)[-1], "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                "pmid": pmid, "title": w.get("title") or "", "year": w.get("publication_year"),
                "journal": src, "cited": w.get("cited_by_count", 0), "authors": auths,
                "grants": [{"funder": g.get("funder_display_name", ""), "award": g.get("award_id") or ""}
                           for g in (w.get("grants") or [])],
            })
        cursor = (js.get("meta") or {}).get("next_cursor")
    return out[:n]


# ---------------------------------------------------------------- NIH RePORTER
RP = "https://api.reporter.nih.gov/v2/projects/search"


def nih_projects(text, n=25, years_back=6):
    from common import TODAY
    body = {
        "criteria": {
            "advanced_text_search": {"operator": "and", "search_field": "projecttitle,terms,abstracttext",
                                     "search_text": text},
            "fiscal_years": list(range(TODAY.year - years_back, TODAY.year + 1)),
        },
        "include_fields": ["ApplId", "ProjectNum", "ProjectTitle", "PrincipalInvestigators", "Organization",
                           "AwardAmount", "FiscalYear", "AgencyIcAdmin", "ProjectStartDate", "ProjectEndDate"],
        "offset": 0, "limit": min(n, 50), "sort_field": "project_start_date", "sort_order": "desc",
    }
    js = get(RP, method="POST", body=body, pause=1.0)
    out = []
    for p in (js or {}).get("results", []):
        org = p.get("organization") or {}
        out.append({
            "appl": p.get("appl_id"), "num": p.get("project_num", ""), "title": p.get("project_title", ""),
            "pis": [x.get("full_name", "") for x in p.get("principal_investigators") or []],
            "org": org.get("org_name", ""), "country": org.get("org_country", ""),
            "amount": p.get("award_amount"), "fy": p.get("fiscal_year"),
            "ic": (p.get("agency_ic_admin") or {}).get("abbreviation", ""),
            "start": (p.get("project_start_date") or "")[:10], "end": (p.get("project_end_date") or "")[:10],
        })
    return out
