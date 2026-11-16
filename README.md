# Market Panopticon

A point-in-time, evidence-backed **power graph** of the economy. Enter a ticker and it rebuilds the forces that can move that company. It answers one question:

> What changed, which entities transmit that change to this stock, how strong is the connection, and what has the market probably not priced in yet?

Panopticon is a **separate system from OptLab**. It does not forward news. It sends OptLab small, versioned, immutable **signal packets** (hypotheses). OptLab then tests them hard.

> **Status:** v0.1 engine running on a **fictional** semiconductor universe (`panopticon/seed.py`). Every company, agency, ticker, number and document there is invented. The engine is real. The data is a stand-in until Tier-1/2/3 ingestion is connected.

## Run it

No dependencies beyond Python 3.11+.

```bash
python -m panopticon serve --port 8000            # UI: http://127.0.0.1:8000
python -m panopticon home HLCN                    # the home screen as JSON
python -m panopticon ask HLCN treated_as_certain  # any of the 10 investigations
python -m panopticon dossier HLCN > dossier.md    # causal dossier
python -m panopticon shock --preset export_restriction
python -m panopticon shock --node CU --change 0.25 --label "Copper +25%"
python -m panopticon signals HLCN --ledger out/signals.jsonl
python -m panopticon --as-of 2026-11-09 home HLCN # replay what was known on a past date
python -m panopticon --feedback examples/optlab_feedback.json signals HLCN
python -m unittest discover -s tests
```

`examples/` holds generated output: a dossier, the home screen, a shock run, and a signal ledger.

## How news is handled

News is **one weak sensor among many**:

| Rule | Where |
|---|---|
| Documents are evidence attached to **claims**. Claims are the primary object, and 34 demo documents reduce to 18 claims. | `claims.py` |
| Syndicated copies with the same `origin_root` count once. | `claims._independent_sources` |
| Sensor tiers are weighted: Tier 1 (0.95) > Tier 2 (0.75) > Tier 3 (0.65) > Tier 4 narrative (0.35). | `model.TIER_WEIGHT` |
| Narrative-only support is capped at 0.35 quality, gets status `narrative_only`, and is **never** `confirmed`, however many outlets repeat it. | `claims.NARRATIVE_ONLY_CAP` |
| Narrative-only claims are never admitted as observations in contradiction mining. | `contradictions._observations` |
| **No packet reaches OptLab without at least one non-narrative claim.** | `signals.packets_from_cone` |
| Narrative sources have one job: estimating **market awareness** (how much is already priced in). | `signals.market_awareness` |

## Architecture (concept section → module)

| Concept | Module | Notes |
|---|---|---|
| §1 Power graph: 26 node types, 21 edge types, strength ≠ confidence | `model.py`, `graph.py` | Edges are **bitemporal**: `effective_from/to` (when it was true) and `recorded_at` (when we knew it). |
| §2 Claims, not news | `claims.py` | Status: `confirmed` / `corroborated` / `single_sensor` / `narrative_only` / `disputed`. Novelty decays against prior claims. |
| §3 Sensor hierarchy | `model.SourceTier` | |
| §4 Six power layers | `graph.NODE_LAYER`, `EDGE_LAYER`, `dossier.six_layers` | An edge type can override the node's layer (a company that `LENDS_TO` you is capital power). |
| §5 Dependency cone, `I(p)=E×D×M×C×T×N` | `cone.py` | Best-first expansion that stops when E×C potential drops below a threshold. **Common-shock rule:** a shock that hits the focal company directly is not also counted as relief through its competitors. |
| §6 Four clocks | `model.Clock`, `cone.HALF_LIFE_DAYS` | Each clock decays at its own rate. Packets have `trading_clock_eligible: false` until OptLab reports a tested translation layer for that signal type. |
| §7 Contradiction mining | `contradictions.py` | Detects management vs operations, consensus vs supply chain, narrative vs evidence, valuation vs policy, and price vs positioning. Needs ≥2 independent observations. |
| §8 Government as a market participant | `government.py` | 11 roles. Pipeline from proposal to economic effect, with stage pass rates, appropriation gaps and expected effect dates. Agencies carry **levers** the shock engine can trigger. |
| §9 Causal dossier | `dossier.py` | Company engine, power map, six layers, sensitivity table, contradictions, policy pipeline, resolution points, exported signals. |
| §10 Shock propagation | `shocks.py` | Returns first, second and third order effects, delayed effects, substitutions, government responses, capital-market consequences, contradictory effects, net exposure and least-aligned expectations. 10 presets. |
| §11 OptLab interface | `signals.py` | Schema `panopticon.signal/1`. Packets are content-hashed. The ledger is append-only and rejects modified packets. `known_as_of()` gives the backtest view. |
| §12 Questions, not feeds | `investigations.py`, `web/index.html` | 10 investigations. Home screen: 3 changes, 2 contradictions, 1 expanding risk path, 1 overlooked beneficiary, resolution points. |
| §13 MVP vertical, 5 event families | `seed.py`, `model.EventFamily` | Semiconductors: 1 focal company, 5 competitors plus a domestic substitute, 10 suppliers, 10 customers, 5 agencies plus 2 committees, commodity and macro nodes, 3 chokepoints, banks, owners and debt. |
| §15 OptLab feedback loop | `signals.FeedbackRegistry` | Per-signal-type weights, what each type predicts, translation-tested flags, and edge-strength multipliers applied to the graph. |

### State convention for propagation
A signed `change` at a node means: **activity or health** for companies and regions, **price** for commodities, **stringency** for regulators, and **spending** for budget-holding agencies. Each edge type has a flow direction (`model.FLOW`). For example, `A EXPOSED_TO copper` means copper moves A, and `A SUPPLIES B` means A's health moves B. `direction` sets the sign.

## Moving from demo to live (in order)

1. **Tier-1 ingestion:** EDGAR (10-K/10-Q/8-K/Form 4/13F), the Federal Register, Congress.gov, USAspending/SAM, tariff and sanctions lists, and BLS/EIA/FRED. Each connector emits `Evidence` plus a `ClaimDraft`. The resolver, cone and everything downstream stay the same.
2. **Entity resolution:** CIK ↔ ticker ↔ LEI ↔ UEI ↔ legal names. Every node needs a stable id before automated edge creation.
3. **Hand-verified edges:** replace `seed.EDGES` with a reviewed relationship table (spec: several hundred). Keep `evidence_ids` and `recorded_at` honest, because they are what make backtests valid.
4. **Market state (Tier 3):** prices, short interest, options skew, estimate revisions. These fill `price_trend_20d`, `analyst_count` and the assumption set (Layer F).
5. **Measured base rates:** `government.STAGE_PASS_RATE` and the clock half-lives are placeholders. Fit them from history.
6. **OptLab loop:** OptLab reads `signals.jsonl` through `known_as_of()`, backtests by signal family, and writes the feedback JSON back.

## Known limitations
- Shock propagation adds path effects and saturates them with `tanh`. Overlapping paths are not modelled as properly dependent.
- Contradiction classes depend on assumptions being entered (`MarketAssumption`). Extracting them automatically from guidance and consensus data is future work.
- The UI is a thin stdlib server for one user. There is no auth and no persistence beyond the JSONL ledger.

## Live pipeline: Micron (MU) from SEC EDGAR

The first live slice. It connects **SEC filings only**, and every live claim must be **approved by a person** before it counts:

```
EDGAR document → immutable raw evidence → candidate claim → human verification → active graph state → dossier
```

**Setup.** The SEC requires a contact name and email in the User-Agent. Supply them only as environment variables. They are never written to disk, logs or the repo:

```bash
export EDGAR_CONTACT_NAME="Your Name"
export EDGAR_CONTACT_EMAIL="you+panopticon@example.com"
```

**Workflow**

```bash
python3 -m panopticon live ingest MU --since 2025-06-01   # filings + XBRL facts -> candidate claims
python3 -m panopticon live status
python3 -m panopticon live review --family INVENTORY      # list pending candidates
python3 -m panopticon live show <claim_id>                # full evidence trail + history
python3 -m panopticon live approve <claim_id> --by "Your Name" --note "checked against 10-Q"
python3 -m panopticon live reject  <claim_id> --by "Your Name" --note "wrong comparison period"
python3 -m panopticon live rel list                       # draft relationship map (all unverified)
python3 -m panopticon live rel verify mu_e000 --by "Your Name" --evidence "10-K FY2026 Item 1A, p.NN"
python3 -m panopticon live dossier MU > MU_dossier.md
python3 -m panopticon live signals MU                     # empty until approved claims sit on verified edges
```

Data is stored in `data/live/` (git-ignored): a SQLite DB plus raw filings named by their sha256 hash. Re-running `ingest` is safe, because filings that are already stored are never downloaded again.

**Claim families:** `INVENTORY` (level, QoQ change, days of inventory), `PRICING_MARGIN` (gross margin), `CAPACITY_CAPEX` (quarterly capex, with Q4 and mid-year quarters derived from year-to-date figures and labelled as derived), `GEO_REGULATORY` (10-K passages on China/CAC, export controls, the CHIPS Act, tariffs, Taiwan/Japan and customer concentration; identical wording across years is one claim, changed wording is a new one), `INSIDER` (Form 4s classified into open-market sale or purchase, grant, tax withholding, option exercise and gift, with the 10b5-1 plan flag; only discretionary open-market trades carry signal), and `CORPORATE_EVENT` (selected 8-K items).

**Acceptance tests** (`tests/test_live.py`, offline). Each of the 10 criteria has its own test: no duplicates on re-run; full provenance kept (accession, acceptance time, period, form, URL, hash); amendments never erase originals; filing time is kept separate from the economic period; Form 4 classification; repeated facts aren't double-counted, and a repeat doesn't shift when a fact first became known; restatements supersede older claims without deleting them; extraction never confirms a claim; replay hides documents not yet filed; unverified edges can't produce packets. A further test checks that the contact email is never persisted.

**Known gaps** (beyond the one-day slice): XBRL evidence cites the tagged fact (concept, period and accession) rather than the exact inline-XBRL element; text extraction uses keyword patterns, not a trained extractor; companyfacts excludes dimensional (segment and geographic) data; only the recent-filings window of the submissions API is read; insider claims are made per filing, not aggregated per insider; the relationship map in `data/relationships/MU.json` is an unverified draft.

### XBRL period normalization and audit

Every XBRL observation is classified **from its dates** as `INSTANT`, `DIRECT_QUARTER`, `YEAR_TO_DATE`, `FISCAL_YEAR`, `COMPARATIVE` or `UNKNOWN` (`panopticon/live/periods.py`). SEC's `fy`/`fp` fields describe the *filing*, not the fact, so they are recorded but never used to classify. Quarter flows are a direct quarterly context (preferred, and cross-checked) or a cumulative difference (Q2 = 6M − Q1, Q3 = 9M − 6M, Q4 = FY − 9M). Inventory is the instant balance on a reporting-period end.

A value is **quarantined** (kept as evidence, but producing no claim, signal or trusted table cell) when: other units are present for the same context (they are ignored rather than mixed in), several incompatible contexts exist for one period, a derivation input is missing, a derived flow is negative, the direct and derived values disagree by more than 0.5%, revenue and gross profit cover different periods, revenue − COGS − gross profit is off by more than 0.5% of revenue, or two revenue concepts report materially different values for the same quarter. When both revenue concepts agree, the primary concept is used and both are kept as lineage.

Every displayed number links to its fact contexts. That includes computed cells: gross margin and days of inventory carry their formula with the actual operands (e.g. `Gross margin = gross profit 35,056 / revenue 41,456 ($M) = 84.5639%`) plus every input observation.

```bash
python3 -m panopticon live audit MU --periods 2025-05-29,2026-05-28,2026-09-03   # full lineage per number
python3 -m panopticon live source-check MU --periods 2026-05-28                  # numeric source-presence check
```

`source-check` (alias `reconcile`) is a **numeric source-presence check with proximity rules, not statement reconciliation**. For each input fact it looks for the value as filed (in $ millions, parentheses for cash outflows) in the reporting filing's text and grades the best occurrence:

| Grade | Meaning |
|---|---|
| `STATEMENT_LINE_MATCH` | under a consolidated-statement heading, right after the expected line-item label, with the period's column date earlier in that statement, in a filing stated in millions |
| `LINE_ITEM_MATCH` | label and date nearby, but not under a statement heading (e.g. an MD&A table) |
| `LABEL_ONLY` / `NUMBER_ONLY` / `MISSING` | progressively weaker; it exits 1 if anything is `MISSING` |

If the original filing predates the ingest window, the check falls back to a later filing that repeats the fact as a comparative column, and says so. Confirming the exact column and line placement is still a manual step: open the filing links from the audit.

`tests/test_periods.py` holds the regression fixtures. Run with `PANOPTICON_OLD_QUARTERS=1` to execute them against the pre-fix implementation loaded from `main`.
