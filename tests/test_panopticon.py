import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from panopticon import seed
from panopticon.claims import NARRATIVE_ONLY_CAP, ClaimDraft, ClaimResolver
from panopticon.cone import build_cone
from panopticon.contradictions import mine
from panopticon.government import PolicyItem, PolicyStage
from panopticon.investigations import QUESTIONS, Investigation
from panopticon.model import Clock, Edge, EdgeType, Entity, Evidence, NodeType, SourceTier
from panopticon.shocks import Shock, propagate
from panopticon.signals import FeedbackRegistry, SignalLedger, SignalPrior, generate, packets_from_cone
from panopticon.graph import PowerGraph

AS_OF = seed.AS_OF
T0 = datetime(2026, 11, 10, tzinfo=timezone.utc)


def ev(i, tier, origin, key="k", pol=1, root=None):
    return Evidence(i, "x", tier, T0, None, origin, key, pol, root)


def draft(key="k"):
    return ClaimDraft(key, "something changed", "PRICING_INPUT_COST_CHANGE", ["A"], T0, "A", "price", 0.1, 0.5)


class ClaimTests(unittest.TestCase):
    def test_syndicated_copies_count_once(self):
        one = ClaimResolver().resolve([draft()], [ev("a", SourceTier.OPERATIONAL, "Ops"),
                                                  ev("n1", SourceTier.NARRATIVE, "Wire", root="Wire")])[0]
        many = ClaimResolver().resolve([draft()], [ev("a", SourceTier.OPERATIONAL, "Ops")] +
                                       [ev(f"n{i}", SourceTier.NARRATIVE, f"Outlet{i}", root="Wire") for i in range(50)])[0]
        self.assertAlmostEqual(one.evidence_quality, many.evidence_quality)

    def test_news_alone_cannot_establish_reality(self):
        c = ClaimResolver().resolve([draft()], [ev(f"n{i}", SourceTier.NARRATIVE, f"Outlet{i}") for i in range(40)])[0]
        self.assertEqual(c.status, "narrative_only")
        self.assertLessEqual(c.evidence_quality, NARRATIVE_ONLY_CAP)

    def test_tier1_confirms_and_disputes_are_detected(self):
        c = ClaimResolver().resolve([draft()], [ev("f", SourceTier.AUTHORITATIVE, "SEC")])[0]
        self.assertEqual(c.status, "confirmed")
        d = ClaimResolver().resolve([draft()], [ev("n", SourceTier.NARRATIVE, "Rumor"),
                                                ev("ir", SourceTier.AUTHORITATIVE, "IR", pol=-1)])[0]
        self.assertEqual(d.status, "disputed")

    def test_novelty_decays_for_repeated_information(self):
        r = ClaimResolver()
        first = r.resolve([draft("a")], [ev("x", SourceTier.AUTHORITATIVE, "S", key="a")])[0]
        second = r.resolve([draft("b")], [ev("y", SourceTier.AUTHORITATIVE, "S", key="b")])[0]
        self.assertEqual(first.novelty, 1.0)
        self.assertLess(second.novelty, 0.1)

    def test_demo_reduces_documents_to_fewer_claims(self):
        g = seed.build()
        self.assertGreater(len(g.evidence), len(g.claims))
        self.assertEqual(g.claims["cl_btec_rule"].status, "confirmed")
        self.assertEqual(g.claims["cl_hlcn_hype"].status, "narrative_only")
        self.assertEqual(g.claims["cl_crvd_cut"].status, "disputed")


class PointInTimeTests(unittest.TestCase):
    def test_edges_recorded_later_are_invisible(self):
        g = PowerGraph()
        for i in "AB":
            g.add_entity(Entity(i, i, NodeType.COMPANY))
        g.add_edge(Edge("A", EdgeType.SUPPLIES, "B", date(2020, 1, 1), None, .5, "positive_dependency", .9, [],
                        date(2026, 1, 1), recorded_at=date(2026, 6, 1)))
        self.assertEqual(list(g.upstream("B", date(2026, 5, 31))), [])
        self.assertEqual(len(list(g.upstream("B", date(2026, 6, 1)))), 1)

    def test_claims_not_yet_observed_are_excluded(self):
        g = seed.build()
        early = build_cone(g, "HLCN", date(2026, 11, 12))
        self.assertNotIn("cl_btec_rule", {s.claim.id for s in early.scored})
        self.assertIn("cl_btec_rule", {s.claim.id for s in build_cone(g, "HLCN", AS_OF).scored})

    def test_packets_never_cite_future_claims(self):
        g = seed.build()
        when = date(2026, 11, 9)
        for p in generate(g, "HLCN", when):
            for cid in p["claim_ids"]:
                self.assertLessEqual(g.claims[cid].first_observed.date(), when)


class ConeTests(unittest.TestCase):
    def setUp(self):
        self.g = seed.build()
        self.cone = build_cone(self.g, "HLCN", AS_OF)

    def test_drought_chain_reaches_gross_margin_with_negative_sign(self):
        chain = [s for s in self.cone.scored if s.claim.id == "cl_drought" and "SBLT" in s.path.nodes]
        self.assertTrue(chain)
        self.assertLess(chain[0].influence, 0)
        self.assertEqual(chain[0].path.nodes, ["DROUGHT", "HYDRO", "PWR", "AL", "SBLT", "HLCN"])
        self.assertIn("gross_margin", chain[0].path.metrics())

    def test_influence_is_product_of_factors(self):
        s = self.cone.scored[0]
        self.assertAlmostEqual(s.influence, s.E * s.D * s.M * s.C * s.T * s.N)

    def test_common_shocks_are_not_counted_as_competitive_relief(self):
        # Kestrel supplies HLCN and Corvid: its trouble is not a win for HLCN via Corvid.
        bad = [s for s in self.cone.scored if s.claim.subject == "KSTL" and "CRVD" in s.path.nodes]
        self.assertEqual(bad, [])

    def test_threshold_bounds_the_cone(self):
        tight = build_cone(self.g, "HLCN", AS_OF, threshold=0.2)
        self.assertLess(len(tight.paths), len(self.cone.paths))
        self.assertTrue(all(p.potential >= 0.2 for p in tight.paths))


class ContradictionTests(unittest.TestCase):
    def setUp(self):
        self.g = seed.build()
        self.cxs = mine(self.g, "HLCN", AS_OF)
        self.kinds = {c.kind for c in self.cxs}

    def test_all_dossier_classes_detected(self):
        for k in ("management_vs_operations", "consensus_vs_supply_chain", "valuation_vs_policy",
                  "price_vs_positioning"):
            self.assertIn(k, self.kinds)

    def test_narrative_claims_are_never_observations(self):
        for c in self.cxs:
            self.assertNotIn("cl_hlcn_hype", c.observation_claim_ids)

    def test_requires_two_independent_observations(self):
        for c in self.cxs:
            if c.kind != "valuation_vs_policy":
                self.assertGreaterEqual(len(set(c.observation_claim_ids)), 2)


class GovernmentTests(unittest.TestCase):
    def test_later_stages_are_more_likely_to_matter(self):
        ps = [PolicyItem("p", "p", "regulation", s).p_effect() for s in list(PolicyStage)[:-1]]
        self.assertEqual(ps, sorted(ps))

    def test_unfunded_subsidy_flags_gap(self):
        p = PolicyItem("p", "p", "subsidy", PolicyStage.COMMITTEE, appropriated=False)
        self.assertTrue(any("appropriations" in gap for gap in p.gaps()))


class ShockTests(unittest.TestCase):
    def test_export_restriction(self):
        g = seed.build()
        r = propagate(g, Shock("BTEC", 0.6, "export"), AS_OF)
        self.assertLess(r.impacts["HLCN"].effect, 0)
        self.assertGreater(r.impacts["RDBT"].effect, 0)
        responses = {x["agency"] for x in r.government_responses()}
        self.assertIn("Defense Procurement Agency", responses)
        d = r.to_dict()
        for key in ("first_order", "second_order", "delayed_effects", "substitutions", "government_responses",
                    "capital_market_consequences", "greatest_net_exposure", "expectations_least_aligned"):
            self.assertIn(key, d)

    def test_every_preset_runs(self):
        g = seed.build()
        for node, change, label in seed.SHOCK_PRESETS.values():
            self.assertTrue(propagate(g, Shock(node, change, label), AS_OF).impacts, label)


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.g = seed.build()
        self.packets = generate(self.g, "HLCN", AS_OF)

    def test_schema(self):
        required = {"signal_id", "generated_at", "security", "signal_type", "direction", "horizon", "confidence",
                    "novelty", "expected_materiality", "market_awareness", "causal_paths", "evidence_ids",
                    "contradicting_evidence_ids", "valid_from", "expires_at", "invalidation_conditions",
                    "model_version", "trading_clock_eligible"}
        self.assertTrue(self.packets)
        for p in self.packets:
            self.assertLessEqual(required, set(p))
            self.assertIn(p["direction"], ("bullish", "bearish", "volatility"))

    def test_deterministic_ids(self):
        again = generate(seed.build(), "HLCN", AS_OF)
        self.assertEqual([p["signal_id"] for p in self.packets], [p["signal_id"] for p in again])

    def test_news_only_never_reaches_optlab(self):
        g = seed.build()
        for cid in list(g.claims):
            if g.claims[cid].status != "narrative_only":
                g.claims[cid] = replace(g.claims[cid], status="narrative_only")
        cone = build_cone(g, "HLCN", AS_OF)
        self.assertEqual(packets_from_cone(g, cone), [])

    def test_trading_clock_gated_by_feedback(self):
        self.assertFalse(any(p["trading_clock_eligible"] for p in self.packets))
        fb = FeedbackRegistry({("REGULATORY_HEADWIND", "*"): SignalPrior(translation_tested=True)})
        ps = generate(seed.build(), "HLCN", AS_OF, fb)
        self.assertTrue(any(p["trading_clock_eligible"] for p in ps if p["signal_type"] == "REGULATORY_HEADWIND"))

    def test_feedback_weight_scales_materiality(self):
        fb = FeedbackRegistry({("REGULATORY_HEADWIND", "*"): SignalPrior(weight=0.0)})
        ps = generate(seed.build(), "HLCN", AS_OF, fb)
        self.assertNotIn("REGULATORY_HEADWIND", {p["signal_type"] for p in ps})

    def test_ledger_is_append_only_and_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            led = SignalLedger(Path(d) / "s.jsonl")
            self.assertEqual(led.append(self.packets), len(self.packets))
            self.assertEqual(led.append(self.packets), 0)
            tampered = dict(self.packets[0], confidence=0.99)
            with self.assertRaises(ValueError):
                led.append([tampered])
            before = datetime(2026, 11, 15, tzinfo=timezone.utc)
            self.assertEqual(led.known_as_of(before), [])

    def test_feedback_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "fb.json"
            p.write_text(json.dumps({"signal_priors": [{"signal_type": "X", "weight": 0.5, "predicts": "volatility"}],
                                     "edge_multipliers": {"SUPPLIES": 0.5}}))
            fb = FeedbackRegistry.load(p)
            self.assertEqual(fb.get("X", "semis").predicts, "volatility")
            g = seed.build()
            before = g.edges[0].strength
            fb.apply_to_graph(g)
            self.assertAlmostEqual(g.edges[0].strength, before * 0.5)


class InvestigationTests(unittest.TestCase):
    def test_home_screen_shape(self):
        h = Investigation(seed.build(), "HLCN", AS_OF).home()
        self.assertEqual(len(h["meaningful_changes"]), 3)
        self.assertEqual(len(h["unresolved_contradictions"]), 2)
        self.assertIsNotNone(h["expanding_risk_path"])
        self.assertEqual(h["overlooked_beneficiary"]["ticker"], "RDBT")
        self.assertTrue(h["upcoming_resolution_points"])

    def test_every_question_answers(self):
        inv = Investigation(seed.build(), "HLCN", AS_OF)
        for k in QUESTIONS:
            self.assertIn("answer", inv.answer(k))

    def test_other_tickers_work(self):
        for t in ("RDBT", "KSTL", "STRC", "SBLT"):
            Investigation(seed.build(), t, AS_OF).home()


if __name__ == "__main__":
    unittest.main()
