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
