"""Demo universe: a FICTIONAL semiconductor ecosystem.

Every company, agency, ticker, number and document here is invented. It exists so
the engine has an economically coherent graph to run on until real Tier-1/2/3
ingestion replaces it. Do not read anything here as a statement about a real firm.

MVP shape (spec section 13):
  1 focal company, 5 competitors (+1 domestic substitute), 10 suppliers,
  10 customers/categories, 5 agencies (+2 committees), 8 commodity/macro nodes,
  3 geographic chokepoints, banks / owners / creditors.
Only the five MVP event families are used.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .claims import ClaimDraft, ClaimResolver
from .government import GovRole, PolicyItem, PolicyStage
from .graph import PowerGraph
from .model import (
    Catalyst, Clock, Edge, EdgeType as R, Entity, EventFamily as F, Evidence, MarketAssumption,
    NodeType as N, SourceTier as T,
)

AS_OF = date(2026, 11, 16)
DEMO = "DEMO DATA - fictional entities"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


ENTITIES = [
    # focal
    ("HLCN", "Halcyon Compute", N.COMPANY, dict(
        ticker="HLCN", industry="semiconductors", analyst_count=38, price_trend_20d=0.14,
        net_debt_to_ebitda=0.8, hq="Domestic",
        engine={
            "how_it_makes_money": "Fabless AI-accelerator designer; ~82% of revenue from data-center "
                                  "accelerators sold to hyperscalers, ~6% from government programs.",
            "production_constraint": "Leading-edge wafer starts at Kestrel Foundry and advanced-packaging "
                                     "capacity at Ardent; HBM allocation from Lumen.",
            "who_ultimately_pays": "Hyperscaler capex budgets, which are funded by cloud AI revenue and "
                                   "debt markets (Atlas Data Centers is levered).",
            "pricing_power": "Software ecosystem lock-in and a 12-18 month performance lead; weakens if "
                             "customers hold excess inventory.",
            "valuation_assumption": "Demand keeps accelerating through 2027 while gross margin expands.",
        },
        lobbying=[{"target": "BTEC", "issue": "export-control carve-outs", "usd_ytd": 2_400_000}])),
    # competitors + substitute
    ("CRVD", "Corvid Silicon", N.COMPETITOR, dict(ticker="CRVD", analyst_count=31)),
    ("MRDL", "Meridian Logic", N.COMPETITOR, dict(ticker="MRDL", analyst_count=22)),
    ("BWS", "Brightwater Semi", N.COMPETITOR, dict(ticker="BWS", analyst_count=12)),
    ("OAKS", "Oakhollow Systems", N.COMPETITOR, dict(ticker="OAKS", analyst_count=9, net_debt_to_ebitda=3.1)),
    ("TSLT", "Tessellate AI (private)", N.COMPETITOR, dict(private=True)),
    ("RDBT", "Redoubt Microsystems", N.COMPETITOR, dict(ticker="RDBT", analyst_count=3,
                                                       note="domestic trusted-accelerator supplier")),
    # suppliers
    ("KSTL", "Kestrel Foundry", N.SUPPLIER, dict(ticker="KSTL", analyst_count=27, geo_concentration=0.85)),
    ("ARDP", "Ardent Packaging", N.SUPPLIER, dict(ticker="ARDP", analyst_count=14, geo_concentration=0.6)),
    ("LUMN", "Lumen Memory", N.SUPPLIER, dict(ticker="LUMN", analyst_count=25)),
    ("PLSB", "Polaris Substrates", N.SUPPLIER, dict(ticker="PLSB", analyst_count=6, geo_concentration=0.7)),
    ("NWPM", "Northwind Photomask (private)", N.SUPPLIER, dict(private=True)),
    ("CBDA", "Cobalt EDA", N.SUPPLIER, dict(ticker="CBDA", analyst_count=18)),
    ("SBLT", "Sable Thermal", N.SUPPLIER, dict(ticker="SBLT", analyst_count=4, net_debt_to_ebitda=3.4)),
    ("VIRG", "Vireo Specialty Gases", N.SUPPLIER, dict(ticker="VIRG", analyst_count=5)),
    ("GRTS", "Granite Test Systems", N.SUPPLIER, dict(ticker="GRTS", analyst_count=11)),
    ("HLVO", "Halvard Optics", N.SUPPLIER, dict(ticker="HLVO", analyst_count=8)),
    # customers
    ("STRC", "Stratus Cloud", N.CUSTOMER, dict(ticker="STRC", analyst_count=45)),
    ("NMBS", "Nimbus Hyperscale", N.CUSTOMER, dict(ticker="NMBS", analyst_count=40)),
    ("ATDC", "Atlas Data Centers", N.CUSTOMER, dict(ticker="ATDC", analyst_count=16, net_debt_to_ebitda=4.2)),
    ("QUIL", "Quill Automotive", N.CUSTOMER, dict(ticker="QUIL", analyst_count=20)),
    ("IRBT", "Ironbridge Telecom", N.CUSTOMER, dict(ticker="IRBT", analyst_count=19)),
    ("JNPE", "Juniper Edge", N.CUSTOMER, dict(ticker="JNPE", analyst_count=7)),
    ("ORCD", "Orchard Devices", N.CUSTOMER, dict(ticker="ORCD", analyst_count=24)),
    ("PNRL", "Pinnacle Research Labs (private)", N.CUSTOMER, dict(private=True)),
    ("SOVAI", "Sovereign AI programs (category)", N.CUSTOMER, {}),
    ("HLCN_AUR", "Aurora accelerator (HLCN product)", N.PRODUCT, {}),
    # government
    ("BTEC", "Bureau of Technology Export Controls", N.GOVERNMENT_AGENCY, dict(
        roles=[GovRole.GATEKEEPER.value, GovRole.SANCTIONS_AUTHORITY.value],
        overseen_by=["CIP"], statute="Strategic Technology Export Act (fictional)",
        active_rulemakings=["Advanced accelerator licensing rule"],
        levers=[{"action": "Issue general licences / carve-outs for allied customers",
                 "protects": ["HLCN", "CRVD"], "benefits": ["HLCN", "CRVD"]}])),
    ("DPA", "Defense Procurement Agency", N.GOVERNMENT_AGENCY, dict(
        roles=[GovRole.CUSTOMER.value, GovRole.TECHNOLOGY_SPONSOR.value],
        overseen_by=["APPS"], statute="Trusted Microelectronics Procurement Act (fictional)",
        levers=[{"action": "Shift trusted-accelerator procurement toward domestic supplier Redoubt",
                 "protects": ["HLCN", "KSTL"], "benefits": ["RDBT"]}])),
    ("DIS", "Department of Industrial Strategy", N.GOVERNMENT_AGENCY, dict(
        roles=[GovRole.FINANCIER.value, GovRole.TECHNOLOGY_SPONSOR.value],
        overseen_by=["CIP", "APPS"],
        levers=[{"action": "Accelerate fab-incentive disbursement / emergency grants",
                 "protects": ["KSTL", "SBLT"], "benefits": ["KSTL"]}])),
    ("CMPA", "Competition & Markets Authority", N.GOVERNMENT_AGENCY, dict(
        roles=[GovRole.GATEKEEPER.value], overseen_by=["CIP"])),
    ("ERC", "Energy Regulatory Commission", N.GOVERNMENT_AGENCY, dict(
        roles=[GovRole.PRICE_SETTER.value],
        levers=[{"action": "Emergency regional power-price cap", "protects": ["ATDC", "SBLT"],
                 "benefits": ["ATDC", "SBLT"]}])),
    ("CIP", "Committee on Industrial Policy", N.LEGISLATIVE_BODY, {}),
    ("APPS", "Appropriations Subcommittee on Commerce & Defense", N.LEGISLATIVE_BODY, {}),
    # commodities / macro
    ("CU", "Copper", N.COMMODITY, {}),
    ("AL", "Aluminum", N.COMMODITY, {}),
    ("NEON", "Neon gas", N.COMMODITY, dict(geo_concentration=0.7)),
    ("HBMP", "HBM memory contract price", N.COMMODITY, {}),
    ("PWR", "Western Grid power price", N.COMMODITY, {}),
    ("FRT", "Trans-Varn freight rate", N.COMMODITY, {}),
    ("RATE", "Policy interest rate", N.ECONOMIC_INDICATOR, {}),
    ("USD", "Trade-weighted dollar", N.ECONOMIC_INDICATOR, {}),
    ("APOB", "Advanced-packaging order book", N.ECONOMIC_INDICATOR, {}),
    ("TECH_CHIPLET", "Open chiplet interconnect standard", N.TECHNOLOGY, {}),
    # physical / geography
    ("DROUGHT", "Northern Basin drought", N.EVENT, {}),
    ("HYDRO", "Northern Hydro Basin", N.REGION, {}),
    ("TESHAN", "Teshan Island fab cluster", N.REGION, dict(geo_concentration=0.9, chokepoint=True)),
    ("VARN", "Strait of Varn", N.PORT, dict(chokepoint=True)),
    ("CALD", "Port of Calder", N.PORT, dict(chokepoint=True)),
    # capital
    ("FMB", "First Meridian Bank", N.BANK, dict(ticker="FMB", analyst_count=26)),
    ("LARCH", "Larchmont Capital (8.1% holder)", N.MAJOR_SHAREHOLDER, {}),
    ("SEMX", "Semiconductor index ETF", N.FUND, {}),
    ("HLCN28", "HLCN 4.25% notes due 2028", N.DEBT_INSTRUMENT, {}),
    # labor / narrative
    ("FABENG", "Fab process engineers", N.LABOR_CATEGORY, {}),
    ("NARR_AI", "Narrative: 'AI capex supercycle'", N.NARRATIVE, {}),
]

P, NEG, AMB = "positive_dependency", "negative_dependency", "ambiguous"
D = date
# (src, rel, tgt, strength, direction, confidence, metrics, lag_days, clock)
EDGES = [
    # --- suppliers -> HLCN (supply health)
    ("KSTL", R.SUPPLIES, "HLCN", .90, P, .93, ["supply", "revenue"], 120, Clock.BUSINESS),
    ("ARDP", R.SUPPLIES, "HLCN", .70, P, .85, ["supply"], 90, Clock.BUSINESS),
    ("LUMN", R.SUPPLIES, "HLCN", .55, P, .90, ["supply"], 60, Clock.BUSINESS),
    ("PLSB", R.SUPPLIES, "HLCN", .35, P, .75, ["supply"], 90, Clock.BUSINESS),
    ("NWPM", R.SUPPLIES, "HLCN", .25, P, .60, ["supply"], 120, Clock.BUSINESS),
    ("CBDA", R.SUPPLIES, "HLCN", .20, P, .80, ["supply"], 180, Clock.BUSINESS),
    ("SBLT", R.SUPPLIES, "HLCN", .30, P, .82, ["gross_margin"], 45, Clock.BUSINESS),
    ("GRTS", R.SUPPLIES, "HLCN", .15, P, .70, ["supply"], 60, Clock.BUSINESS),
    ("HLVO", R.SUPPLIES, "HLCN", .20, P, .65, ["supply"], 60, Clock.BUSINESS),
    ("VIRG", R.SUPPLIES, "KSTL", .40, P, .70, ["supply"], 60, Clock.BUSINESS),
    ("PLSB", R.SUPPLIES, "ARDP", .50, P, .80, ["supply"], 60, Clock.BUSINESS),
    ("KSTL", R.SUPPLIES, "CRVD", .70, P, .90, ["supply"], 120, Clock.BUSINESS),
    ("KSTL", R.SUPPLIES, "MRDL", .50, P, .85, ["supply"], 120, Clock.BUSINESS),
    ("ARDP", R.SUPPLIES, "CRVD", .50, P, .80, ["supply"], 90, Clock.BUSINESS),
    # --- HLCN -> its customers (shortages travel downstream)
    ("HLCN", R.SUPPLIES, "STRC", .30, P, .85, ["supply"], 60, Clock.BUSINESS),
    ("HLCN", R.SUPPLIES, "NMBS", .30, P, .85, ["supply"], 60, Clock.BUSINESS),
    ("HLCN", R.SUPPLIES, "ATDC", .40, P, .80, ["supply"], 60, Clock.BUSINESS),
    ("HLCN", R.SUPPLIES, "SOVAI", .50, P, .60, ["supply"], 90, Clock.BUSINESS),
    # --- HLCN demand -> supplier revenue
    ("HLCN", R.BUYS_FROM, "KSTL", .45, P, .90, ["revenue"], 90, Clock.BUSINESS),
    ("HLCN", R.BUYS_FROM, "ARDP", .50, P, .85, ["revenue"], 90, Clock.BUSINESS),
    ("HLCN", R.BUYS_FROM, "SBLT", .55, P, .85, ["revenue"], 60, Clock.BUSINESS),
    ("HLCN", R.BUYS_FROM, "LUMN", .30, P, .85, ["revenue"], 60, Clock.BUSINESS),
    ("HLCN", R.BUYS_FROM, "PLSB", .30, P, .75, ["revenue"], 90, Clock.BUSINESS),
    # --- customers' demand -> HLCN
    ("STRC", R.BUYS_FROM, "HLCN", .35, P, .90, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("NMBS", R.BUYS_FROM, "HLCN", .30, P, .90, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("ATDC", R.BUYS_FROM, "HLCN", .12, P, .80, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("SOVAI", R.BUYS_FROM, "HLCN", .10, P, .60, ["demand", "revenue"], 120, Clock.BUSINESS),
    ("ORCD", R.BUYS_FROM, "HLCN", .06, P, .75, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("QUIL", R.BUYS_FROM, "HLCN", .05, P, .70, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("IRBT", R.BUYS_FROM, "HLCN", .04, P, .70, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("JNPE", R.BUYS_FROM, "HLCN", .03, P, .60, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("PNRL", R.BUYS_FROM, "HLCN", .03, P, .50, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("STRC", R.BUYS_FROM, "CRVD", .30, P, .85, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("NMBS", R.BUYS_FROM, "MRDL", .25, P, .80, ["demand", "revenue"], 90, Clock.BUSINESS),
    ("APOB", R.LEADS, "HLCN", .45, P, .75, ["demand", "revenue"], 120, Clock.BUSINESS),
    ("APOB", R.LEADS, "CRVD", .40, P, .70, ["demand", "revenue"], 120, Clock.BUSINESS),
    # --- input prices
    ("HLCN", R.EXPOSED_TO, "HBMP", .45, NEG, .88, ["gross_margin"], 45, Clock.BUSINESS),
    ("LUMN", R.EXPOSED_TO, "HBMP", .70, P, .90, ["revenue"], 30, Clock.BUSINESS),
    ("HLCN", R.EXPOSED_TO, "FRT", .12, NEG, .80, ["gross_margin"], 30, Clock.BUSINESS),
    ("SBLT", R.EXPOSED_TO, "AL", .60, NEG, .85, ["gross_margin"], 30, Clock.BUSINESS),
    ("SBLT", R.EXPOSED_TO, "CU", .35, NEG, .80, ["gross_margin"], 30, Clock.BUSINESS),
    ("HLVO", R.EXPOSED_TO, "CU", .30, NEG, .70, ["gross_margin"], 30, Clock.BUSINESS),
    ("KSTL", R.EXPOSED_TO, "NEON", .30, NEG, .75, ["supply"], 60, Clock.BUSINESS),
    ("VIRG", R.EXPOSED_TO, "NEON", .50, P, .75, ["revenue"], 30, Clock.BUSINESS),
    ("ATDC", R.EXPOSED_TO, "PWR", .50, NEG, .85, ["gross_margin"], 30, Clock.BUSINESS),
    # drought -> hydro -> power -> aluminum chain
    ("HYDRO", R.EXPOSED_TO, "DROUGHT", .80, NEG, .85, [], 30, Clock.STRUCTURAL),
    ("PWR", R.EXPOSED_TO, "HYDRO", .70, NEG, .80, [], 14, Clock.BUSINESS),
    ("AL", R.EXPOSED_TO, "PWR", .60, P, .85, [], 30, Clock.BUSINESS),
    ("ERC", R.REGULATES, "PWR", .30, NEG, .70, [], 60, Clock.BUSINESS),
    # --- geography and logistics
    ("KSTL", R.DEPENDS_ON, "TESHAN", .85, P, .95, ["supply"], 0, Clock.STRUCTURAL),
    ("ARDP", R.DEPENDS_ON, "TESHAN", .60, P, .90, ["supply"], 0, Clock.STRUCTURAL),
    ("PLSB", R.DEPENDS_ON, "TESHAN", .50, P, .85, ["supply"], 0, Clock.STRUCTURAL),
    ("HLCN", R.SHIPS_THROUGH, "VARN", .35, P, .80, ["supply"], 21, Clock.MARKET),
    ("FRT", R.EXPOSED_TO, "VARN", .60, NEG, .80, [], 7, Clock.MARKET),
    ("HLCN", R.SHIPS_THROUGH, "CALD", .20, P, .80, ["supply"], 14, Clock.MARKET),
    ("ATDC", R.SHIPS_THROUGH, "CALD", .15, P, .70, ["supply"], 14, Clock.MARKET),
    # --- competition / substitution
    ("HLCN", R.COMPETES_WITH, "CRVD", .45, NEG, .85, ["revenue"], 90, Clock.BUSINESS),
    ("HLCN", R.COMPETES_WITH, "MRDL", .30, NEG, .80, ["revenue"], 90, Clock.BUSINESS),
    ("HLCN", R.COMPETES_WITH, "BWS", .15, NEG, .75, ["revenue"], 120, Clock.BUSINESS),
    ("HLCN", R.COMPETES_WITH, "OAKS", .10, NEG, .70, ["revenue"], 120, Clock.BUSINESS),
    ("HLCN", R.COMPETES_WITH, "TSLT", .20, NEG, .50, ["revenue"], 180, Clock.BUSINESS),
    ("RDBT", R.SUBSTITUTES_FOR, "HLCN", .25, NEG, .70, ["revenue"], 120, Clock.BUSINESS),
    ("HLCN", R.EXPOSED_TO, "TECH_CHIPLET", .20, P, .60, ["gross_margin"], 270, Clock.STRUCTURAL),
    ("RDBT", R.EXPOSED_TO, "TECH_CHIPLET", .40, P, .60, ["revenue"], 270, Clock.STRUCTURAL),
    ("ARDP", R.EXPOSED_TO, "TECH_CHIPLET", .30, P, .65, ["revenue"], 270, Clock.STRUCTURAL),
    # --- government
    ("BTEC", R.REGULATES, "HLCN", .45, NEG, .90, ["revenue", "demand"], 30, Clock.BUSINESS),
    ("BTEC", R.REGULATES, "CRVD", .40, NEG, .90, ["revenue"], 30, Clock.BUSINESS),
    ("BTEC", R.REGULATES, "MRDL", .20, NEG, .85, ["revenue"], 30, Clock.BUSINESS),
    ("DPA", R.CONTRACTS_WITH, "HLCN", .15, P, .85, ["revenue"], 180, Clock.BUSINESS),
    ("DPA", R.CONTRACTS_WITH, "RDBT", .65, P, .80, ["revenue"], 120, Clock.BUSINESS),
    ("DIS", R.SUBSIDIZES, "KSTL", .30, P, .70, ["supply"], 365, Clock.STRUCTURAL),
    ("DIS", R.SUBSIDIZES, "RDBT", .20, P, .60, ["revenue"], 365, Clock.STRUCTURAL),
    ("CMPA", R.REGULATES, "HLCN", .10, NEG, .60, ["revenue"], 365, Clock.STRUCTURAL),
    ("APPS", R.SUBSIDIZES, "DPA", .80, P, .90, [], 90, Clock.STRUCTURAL),
    ("APPS", R.SUBSIDIZES, "DIS", .80, P, .90, [], 90, Clock.STRUCTURAL),
    ("HLCN", R.LOBBIES, "BTEC", .10, AMB, .70, [], 0, Clock.STRUCTURAL),
    ("STRC", R.LOBBIES, "BTEC", .10, AMB, .65, [], 0, Clock.STRUCTURAL),
    # --- capital
    ("FMB", R.LENDS_TO, "HLCN", .10, P, .90, ["liquidity"], 30, Clock.BUSINESS),
    ("FMB", R.LENDS_TO, "ATDC", .50, P, .90, ["liquidity"], 30, Clock.BUSINESS),
    ("FMB", R.LENDS_TO, "SBLT", .45, P, .85, ["liquidity"], 30, Clock.BUSINESS),
    ("FMB", R.LENDS_TO, "OAKS", .35, P, .85, ["liquidity"], 30, Clock.BUSINESS),
    ("FMB", R.EXPOSED_TO, "RATE", .30, P, .80, [], 30, Clock.MARKET),
    ("ATDC", R.EXPOSED_TO, "RATE", .50, NEG, .85, ["liquidity"], 30, Clock.MARKET),
    ("OAKS", R.EXPOSED_TO, "RATE", .30, NEG, .80, ["liquidity"], 30, Clock.MARKET),
    ("HLCN", R.EXPOSED_TO, "RATE", .15, NEG, .70, ["valuation"], 30, Clock.MARKET),
    ("HLCN", R.EXPOSED_TO, "USD", .25, NEG, .70, ["revenue"], 120, Clock.BUSINESS),
    ("LARCH", R.OWNS, "HLCN", .12, P, .95, [], 0, Clock.MARKET),
    ("SEMX", R.OWNS, "HLCN", .15, P, .95, [], 0, Clock.MARKET),
    ("SEMX", R.OWNS, "KSTL", .12, P, .95, [], 0, Clock.MARKET),
    ("HLCN28", R.EXPOSED_TO, "HLCN", .60, P, .90, [], 0, Clock.MARKET),
    # --- labor / narrative
    ("KSTL", R.EMPLOYS, "FABENG", .30, P, .70, ["supply"], 90, Clock.BUSINESS),
    ("NARR_AI", R.CORRELATED_WITH, "HLCN", .30, P, .55, ["valuation"], 0, Clock.MARKET),
]

# Evidence: (id, source_type, tier, published, origin, claim_key, polarity, origin_root, excerpt)
EVIDENCE = [
    # export restriction: two Tier-1 records, one 8-K, and 4 news items (3 are one wire story)
    ("ev_fr_2211", "federal_register", T.AUTHORITATIVE, "2026-11-13T15:08", "Federal Register", "btec_rule", 1, None,
     "Interim final rule: licence requirement for accelerators above performance threshold to Region Q"),
    ("ev_btec_pr7", "agency_release", T.AUTHORITATIVE, "2026-11-13T15:10", "BTEC", "btec_rule", 1, None, ""),
    ("ev_hlcn_8k", "sec_8k", T.AUTHORITATIVE, "2026-11-14T21:30", "HLCN IR", "btec_rule", 1, None,
     "HLCN estimates restricted products were 9-11% of trailing revenue"),
    ("ev_n_101", "news", T.NARRATIVE, "2026-11-13T15:40", "WireCo", "btec_rule", 1, "WireCo", ""),
    ("ev_n_102", "news", T.NARRATIVE, "2026-11-13T15:55", "Daily Ledger", "btec_rule", 1, "WireCo", ""),
    ("ev_n_103", "news", T.NARRATIVE, "2026-11-13T16:02", "Market Minute", "btec_rule", 1, "WireCo", ""),
    ("ev_tp_104", "trade_publication", T.NARRATIVE, "2026-11-14T09:00", "Silicon Weekly", "btec_rule", 1, None, ""),
    # procurement
    ("ev_usas_88", "procurement_record", T.AUTHORITATIVE, "2026-11-06T12:00", "Procurement Portal", "dpa_budget", 1, None,
     "FY27 trusted-accelerator line item +40%"),
    ("ev_apps_rpt", "congressional_record", T.AUTHORITATIVE, "2026-11-05T18:00", "Appropriations report", "dpa_budget", 1,
     None, "Funds appropriated; obligation expected Q1 2027"),
    # capacity
    ("ev_permit_31", "government_permit", T.OPERATIONAL, "2026-11-10T00:00", "County permit DB", "kstl_permit", 1, None,
     "Arden Valley fab expansion: status 'pending - supplemental review'"),
    ("ev_env_32", "environmental_filing", T.OPERATIONAL, "2026-11-11T00:00", "State env. agency", "kstl_permit", 1, None, ""),
    ("ev_jobs_77", "job_postings", T.OPERATIONAL, "2026-11-12T00:00", "Job-posting panel", "kstl_hiring", 1, None,
     "Process-engineer postings -18% QoQ at Arden Valley"),
    ("ev_lead_51", "supplier_lead_times", T.OPERATIONAL, "2026-11-08T00:00", "Distributor quotes", "ardp_leadtime", 1, None,
     "Advanced packaging lead time 14 -> 22 weeks"),
    ("ev_lead_52", "supplier_lead_times", T.OPERATIONAL, "2026-11-09T00:00", "Broker survey", "ardp_leadtime", 1, None, ""),
    # pricing / input cost
    ("ev_met_12", "official_statistics", T.AUTHORITATIVE, "2026-10-20T00:00", "Met. Service", "drought", 1, None,
     "Northern Basin reservoirs 38% below seasonal normal"),
    ("ev_lme_9", "exchange_data", T.AUTHORITATIVE, "2026-11-11T00:00", "Metals exchange", "aluminum", 1, None, ""),
    ("ev_lumn_ir", "ir_document", T.AUTHORITATIVE, "2026-11-04T13:00", "LUMN IR", "hbm_price", 1, None,
     "Q1 contract pricing +12%"),
    ("ev_n_140", "news", T.NARRATIVE, "2026-11-04T14:00", "Chip Desk", "hbm_price", 1, None, ""),
    ("ev_ship_3", "shipping_data", T.OPERATIONAL, "2026-11-09T00:00", "AIS freight index", "freight", 1, None, ""),
    ("ev_cust_4", "port_activity", T.OPERATIONAL, "2026-11-10T00:00", "Port authority", "freight", 1, None, ""),
    # demand
    ("ev_strc_10q", "sec_10q", T.AUTHORITATIVE, "2026-11-03T21:00", "STRC IR", "strc_inventory", 1, None,
     "Accelerator inventory +30% QoQ"),
    ("ev_ardp_10q", "sec_10q", T.AUTHORITATIVE, "2026-11-05T21:00", "ARDP IR", "apob", 1, None,
     "Advanced-packaging backlog -15% QoQ"),
    # narrative-only (should never become an observation or a signal)
    ("ev_n_201", "news", T.NARRATIVE, "2026-11-12T10:00", "WireCo", "hlcn_hype", 1, "WireCo",
     "HLCN 'sold out through 2027'"),
    ("ev_n_202", "news", T.NARRATIVE, "2026-11-12T10:20", "Daily Ledger", "hlcn_hype", 1, "WireCo", ""),
    ("ev_n_203", "social", T.NARRATIVE, "2026-11-12T11:00", "Social media", "hlcn_hype", 1, None, ""),
    ("ev_n_204", "analyst_commentary", T.NARRATIVE, "2026-11-13T08:00", "Sell-side note", "hlcn_hype", 1, None, ""),
    # capital positioning
    ("ev_si_6", "short_interest", T.CAPITAL, "2026-11-12T22:00", "Exchange SI file", "hlcn_short", 1, None,
     "Short interest +41% over two settlement periods"),
    ("ev_f4_19", "sec_form4", T.AUTHORITATIVE, "2026-11-10T22:00", "SEC Form 4", "hlcn_insider", 1, None,
     "3 executives net sold $84M outside 10b5-1 plans"),
    ("ev_sloos_2", "official_statistics", T.AUTHORITATIVE, "2026-11-07T19:00", "Central bank survey", "fmb_tighten", 1,
     None, "Tightening standards for data-center construction loans"),
    # disputed rumour
    ("ev_n_300", "news", T.NARRATIVE, "2026-11-11T09:00", "Rumor Mill", "crvd_cut", 1, None,
     "Corvid to cut accelerator prices 20%"),
    ("ev_crvd_ir", "ir_document", T.AUTHORITATIVE, "2026-11-11T13:00", "CRVD IR", "crvd_cut", -1, None,
     "Company denies pricing report"),
    # priors (older, for novelty)
    ("ev_lumn_ir_old", "ir_document", T.AUTHORITATIVE, "2026-09-02T13:00", "LUMN IR", "hbm_price_old", 1, None, ""),
    ("ev_met_old", "official_statistics", T.AUTHORITATIVE, "2026-09-15T00:00", "Met. Service", "drought_watch", 1, None, ""),
    # policy pipeline
    ("ev_bill_44", "congressional_record", T.AUTHORITATIVE, "2026-10-28T00:00", "Congress record", "policy_fab", 1, None,
     "Domestic Fabrication Incentive Act reported out of subcommittee"),
]

DRAFTS_PRIOR = [
    ClaimDraft("hbm_price_old", "HBM contract prices raised 8% for Q4", F.PRICING.value, ["LUMN", "HBMP"],
               _dt("2026-09-02T13:00"), "HBMP", "price", 0.08, 0.6),
    ClaimDraft("drought_watch", "Drought watch issued for Northern Basin", F.PRICING.value, ["DROUGHT"],
               _dt("2026-09-15T00:00"), "DROUGHT", "severity", 0.2, 0.3, Clock.STRUCTURAL),
]

DRAFTS = [
    ClaimDraft("btec_rule", "BTEC imposed a licence requirement on high-end accelerators to Region Q",
               F.REGULATORY.value, ["BTEC", "HLCN", "CRVD", "MRDL"], _dt("2026-11-13T15:00"),
               "BTEC", "stringency", 0.6, 0.8),
    ClaimDraft("dpa_budget", "DPA FY27 trusted-accelerator procurement line raised 40% (appropriated)",
               F.PROCUREMENT.value, ["DPA", "APPS", "RDBT", "HLCN"], _dt("2026-11-05T18:00"),
               "DPA", "spending", 0.4, 0.6),
    ClaimDraft("kstl_permit", "Kestrel's Arden Valley fab expansion permit still pending; tool-in slips",
               F.CAPACITY.value, ["KSTL"], _dt("2026-11-10T00:00"), "KSTL", "capacity", -0.25, 0.7),
    ClaimDraft("kstl_hiring", "Kestrel fab process-engineer job postings down 18% QoQ",
               F.CAPACITY.value, ["KSTL", "FABENG"], _dt("2026-11-12T00:00"), "KSTL", "hiring", -0.15, 0.5),
    ClaimDraft("ardp_leadtime", "Ardent Packaging lead times extended from 14 to 22 weeks",
               F.CAPACITY.value, ["ARDP"], _dt("2026-11-08T00:00"), "ARDP", "lead_time", -0.2, 0.6),
    ClaimDraft("drought", "Northern Basin drought declared; reservoirs 38% below normal",
               F.PRICING.value, ["DROUGHT", "HYDRO"], _dt("2026-10-20T00:00"), "DROUGHT", "severity", 0.5, 0.4),
    ClaimDraft("aluminum", "Aluminum price up 18% over six weeks", F.PRICING.value, ["AL"],
               _dt("2026-11-11T00:00"), "AL", "price", 0.18, 0.5),
    ClaimDraft("hbm_price", "Lumen raised HBM contract prices 12% for Q1", F.PRICING.value, ["LUMN", "HBMP"],
               _dt("2026-11-04T13:00"), "HBMP", "price", 0.12, 0.7),
    ClaimDraft("freight", "Trans-Varn freight rates up 22% on strait congestion", F.PRICING.value, ["FRT", "VARN"],
               _dt("2026-11-09T00:00"), "FRT", "price", 0.22, 0.4, Clock.MARKET),
    ClaimDraft("strc_inventory", "Stratus Cloud accelerator inventory up 30% QoQ (purchasing to slow)",
               F.CAPACITY.value, ["STRC", "HLCN"], _dt("2026-11-03T21:00"), "STRC", "purchasing", -0.25, 0.75),
    ClaimDraft("apob", "Advanced-packaging order book down 15% QoQ", F.CAPACITY.value, ["APOB", "ARDP"],
               _dt("2026-11-05T21:00"), "APOB", "orders", -0.15, 0.7),
    ClaimDraft("hlcn_hype", "Media: HLCN demand 'surging', sold out through 2027", F.CAPACITY.value, ["HLCN"],
               _dt("2026-11-12T10:00"), "HLCN", "demand", 0.2, 0.5, Clock.MARKET),
    ClaimDraft("hlcn_short", "HLCN short interest up 41% over two settlement periods", F.CAPITAL.value, ["HLCN"],
               _dt("2026-11-12T22:00"), "HLCN", "short_interest", -0.2, 0.6, Clock.MARKET),
    ClaimDraft("hlcn_insider", "Three HLCN executives net sold $84M outside 10b5-1 plans", F.CAPITAL.value,
               ["HLCN"], _dt("2026-11-10T22:00"), "HLCN", "insider_flow", -0.15, 0.6, Clock.MARKET),
    ClaimDraft("fmb_tighten", "Banks tightening standards for data-center construction loans", F.CAPITAL.value,
               ["FMB", "ATDC"], _dt("2026-11-07T19:00"), "FMB", "lending", -0.2, 0.5, Clock.MARKET),
    ClaimDraft("crvd_cut", "Corvid to cut accelerator prices 20%", F.PRICING.value, ["CRVD", "HLCN"],
               _dt("2026-11-11T09:00"), "CRVD", "share", 0.1, 0.6, Clock.MARKET),
]

ASSUMPTIONS = [
    MarketAssumption("as_demand", "HLCN", "Demand is accelerating into 2027", "demand", +1,
                     "management_guidance", crowdedness=0.8),
    MarketAssumption("as_margin", "HLCN", "Gross margin expands ~150bp in FY27", "gross_margin", +1,
                     "consensus", crowdedness=0.7),
    MarketAssumption("as_supply", "HLCN", "HLCN can double accelerator supply by Q3 2027", "supply", +1,
                     "sell_side", crowdedness=0.6),
    MarketAssumption("as_subsidy", "HLCN", "Subsidised domestic Kestrel fab de-risks HLCN supply by 2027",
                     "supply", +1, "market_narrative", crowdedness=0.5, depends_on_policy="pol_fab"),
]

POLICIES = [
    PolicyItem("pol_export", "Advanced accelerator licensing rule", "export_control", PolicyStage.FINAL_RULE,
               path="executive", agency="BTEC", statute="Strategic Technology Export Act (fictional)",
               stage_entered=D(2026, 11, 13), evidence_ids=["ev_fr_2211"],
               affects={"HLCN": -0.45, "CRVD": -0.4, "MRDL": -0.2}),
    PolicyItem("pol_trusted", "Trusted Accelerator Procurement Program FY27", "procurement",
               PolicyStage.ENFORCEMENT, path="legislative", agency="DPA", committees=["APPS"],
               appropriated=True, stage_entered=D(2026, 11, 5), evidence_ids=["ev_apps_rpt", "ev_usas_88"],
               affects={"RDBT": 0.65, "HLCN": 0.15}),
    PolicyItem("pol_fab", "Domestic Fabrication Incentive Act", "subsidy", PolicyStage.COMMITTEE,
               path="legislative", agency="DIS", committees=["CIP", "APPS"], appropriated=False,
               stage_entered=D(2026, 10, 28), evidence_ids=["ev_bill_44"],
               affects={"KSTL": 0.3, "RDBT": 0.2}),
]

CATALYSTS = [
    Catalyst("HLCN", D(2026, 12, 4), "earnings", "HLCN fiscal Q3 earnings and FY27 guide", ["as_demand", "as_margin"]),
    Catalyst("HLCN", D(2026, 12, 15), "rulemaking", "BTEC licensing rule comment period closes", ["pol_export"]),
    Catalyst("HLCN", D(2027, 1, 20), "capacity", "Kestrel Arden Valley permit hearing", ["as_supply"]),
    Catalyst("RDBT", D(2027, 2, 10), "contract_award", "DPA trusted-accelerator award", ["pol_trusted"]),
    Catalyst("HLCN", D(2027, 2, 10), "contract_award", "DPA trusted-accelerator award", ["pol_trusted"]),
    Catalyst("HLCN", D(2027, 3, 15), "product", "Aurora 2 launch", []),
    Catalyst("HLCN", D(2027, 7, 1), "capacity", "Kestrel Arden Valley capacity originally due online", ["as_supply"]),
    Catalyst("HLCN", D(2028, 3, 1), "debt_maturity", "HLCN 4.25% notes mature", []),
]

SHOCK_PRESETS = {
    "copper": ("CU", 0.25, "Copper rises 25%"),
    "tariff": ("TESHAN", -0.30, "Tariff enacted on Teshan-fabricated chips"),
    "supplier_fails": ("ARDP", -0.80, "Major supplier (Ardent Packaging) fails"),
    "rates_down": ("RATE", -0.25, "Interest rates fall 100 bp"),
    "procurement_cancel": ("DPA", -0.60, "Agency cancels trusted-accelerator procurement program"),
    "competitor_price_cut": ("CRVD", 0.30, "Competitor (Corvid) cuts prices to gain share"),
    "port_closed": ("VARN", -0.50, "Strait of Varn closes for two weeks"),
    "tech_cost_down": ("TECH_CHIPLET", 0.30, "Open chiplet standard cuts unit cost 30%"),
    "export_restriction": ("BTEC", 0.60, "Export restriction on high-end accelerators"),
    "drought": ("DROUGHT", 0.60, "Severe drought in Northern Basin"),
}


def build() -> PowerGraph:
    g = PowerGraph()
    for eid, name, ntype, attrs in ENTITIES:
        g.add_entity(Entity(eid, name, ntype, {**attrs, "provenance": DEMO}))
    for i, (s, rel, t, st, dr, cf, metrics, lag, clock) in enumerate(EDGES):
        g.add_edge(Edge(s, rel, t, effective_from=D(2025, 1, 1), effective_to=None, strength=st, direction=dr,
                        confidence=cf, evidence_ids=[f"ev_rel_{i:03d}"], last_verified=D(2026, 10, 1),
                        lag_days=lag, affected_metrics=metrics, clock=clock, recorded_at=D(2025, 1, 15),
                        verified=True, verified_by="demo"))
    evs = [Evidence(id=i, source_type=st, tier=tier, published=_dt(pub), url=None, origin=origin, claim_key=key,
                    polarity=pol, origin_root=root, excerpt=ex)
           for i, st, tier, pub, origin, key, pol, root, ex in EVIDENCE]
    for ev in evs:
        g.add_evidence(ev)
    resolver = ClaimResolver()
    for c in resolver.resolve(DRAFTS_PRIOR, evs):
        g.add_claim(c)
    for c in resolver.resolve(DRAFTS, evs):
        g.add_claim(c)
    for a in ASSUMPTIONS:
        g.add_assumption(a)
    for p in POLICIES:
        g.policies[p.id] = p
    for c in CATALYSTS:
        g.add_catalyst(c)
    return g
