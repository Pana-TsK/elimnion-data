# elimnion-data

Free data pipeline for the Elimnion Deal Engine. GitHub Actions query public databases
(ClinicalTrials.gov API v2, OpenAlex, NIH RePORTER) and commit compact files that the
Deal Engine agents read with git — no paid search tools in the loop.

| File | What it is | Used by |
|---|---|---|
| `watchlist.json` | Assets and engine orgs to map. Only `drugs` terms are sent to public APIs (empty for anything under CDA). | written by the agents |
| `data/networks/<asset>.json` | Deep network in the Deal Engine `networks` schema (≤700 nodes / 1600 edges) | Network Builder |
| `data/networks/nct-<nct>.json` | Map for each of the top 25 sweep candidates (A, B, D with a coded drug) | Lead Prospector, Network Builder |
| `data/networks/_hubs.json` | People and institutions shared by two or more assets | Network Builder, Job Worker |
| `data/leads/candidates.json` | Deterministic Phase-2-gap sweep (categories A–D), pre-scored | Lead Prospector |
| `data/_status/*.json` | Call counts and errors of the last run | debugging |

Runs daily at 02:23 UTC, on every push to `watchlist.json` or `pipeline/`, and on demand
(Actions → refresh → Run workflow). Add `[sweep]` to a commit message to also rerun the lead sweep on push.
Optional secret `OPENALEX_API_KEY` raises OpenAlex limits.
