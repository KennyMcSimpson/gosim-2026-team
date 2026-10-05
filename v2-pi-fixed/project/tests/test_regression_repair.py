from __future__ import annotations

import copy
import os
import unittest
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.advisor import EventAdvisor
from agent_core.pro_advisor import PlanReviewer
from agent_core.planner import Planner
from test_pro_advisor import _Client, _Planner, _Reviewer, _Trace, _plan


class RegressionRepairTests(unittest.TestCase):
    def setUp(self):
        self.context = {'season_quarter': 1, 'quality_level': 0.75, 'invalidated_actions': 0,
                        'hit_rate_band': 1.0, 'requests': [
                            {'id': 'rq-1', 'remaining': 5, 'deadline': '2026-10-07', 'reward': 100}]}

    def test_request_progress_does_not_expire_global_feedback(self):
        current = copy.deepcopy(self.context)
        current['requests'][0]['remaining'] = 2
        current['quality_level'] = 1.0
        self.assertTrue(EventAdvisor._feedback_fresh(self.context, current))

    def test_feedback_expires_at_material_state_changes(self):
        for field, value in [('season_quarter', 2), ('invalidated_actions', 1),
                             ('hit_rate_band', 0.5), ('quality_level', 1.25), ('requests', [])]:
            current = copy.deepcopy(self.context)
            current[field] = value
            self.assertFalse(EventAdvisor._feedback_fresh(self.context, current), field)
        current = copy.deepcopy(self.context)
        current['requests'][0]['id'] = 'rq-2'
        self.assertFalse(EventAdvisor._feedback_fresh(self.context, current))

    def test_notice_quota_cannot_consume_feedback_quota(self):
        from types import SimpleNamespace
        advisor = EventAdvisor(SimpleNamespace(calls_made=0, max_calls=64), _Trace(), lambda x: None)
        advisor.role_questions['notice_interpretation'] = 14
        advisor._run = lambda: None
        advisor._submit('feedback_adaptation', 0, {}, 1500, 2)
        advisor.worker.join()
        self.assertEqual(advisor.role_questions['feedback_adaptation'], 1)
        self.assertIn('feedback_adaptation', advisor.pending)
        advisor.close()

    def test_all_review_metrics_must_preserve_baseline(self):
        baseline = _plan(duration=600)['metrics']
        candidate = _plan(duration=300)['metrics']
        self.assertTrue(PlanReviewer._tradeoff(baseline, candidate, 'opportunity'))
        for key in ('utility_per_second', 'required_completed', 'required_last_window',
                    'request_reward', 'valid_probability'):
            bad = dict(candidate)
            bad[key] = baseline[key] - 0.1
            self.assertFalse(PlanReviewer._tradeoff(baseline, bad, 'opportunity'), key)
        bad = dict(candidate, science_gain=1)
        self.assertFalse(PlanReviewer._tradeoff(baseline, bad, 'opportunity'))

    def test_empty_frontier_does_not_delay_later_feasible_review(self):
        from datetime import datetime, timedelta
        now = datetime.fromisoformat('2026-10-06T02:00:00+00:00')
        planner, baseline, client = _Planner(), _plan(duration=600), _Client()
        reviewer = _Reviewer(client, _Trace())
        reviewer.select(planner, [], baseline, now, now + timedelta(hours=8), 0)
        reviewer.select(planner, [_plan(duration=300, az=190)], baseline, now, now + timedelta(hours=8), 0)
        self.assertEqual(client.calls_made, 1)

    def make_fault_planner(self, original=False):
        planner = object.__new__(Planner)
        evidence = namedtuple('Evidence', 'drop dark_checks dark_matched')(0.4, 8, 6)
        planner.state = SimpleNamespace(force_program=None, fault_evidence=lambda: evidence,
                                        forget_quality_history=lambda: None)
        planner.reports = 0
        planner.last_report_hours = float('-inf')
        planner.suspicion_hours = []
        planner._fault_vetoes = 0
        planner._original_v2 = original
        planner.advisor = SimpleNamespace(pending=set())
        planner.clock = SimpleNamespace(wall_remaining=lambda: 1500)
        planner.llm = SimpleNamespace(ask_json=lambda *args: {'report': False})
        planner.log = lambda x: None
        return planner

    def test_model_cannot_permanently_veto_renewed_multinight_fault_evidence(self):
        planner = self.make_fault_planner()
        for hours in (0, 6, 12, 36, 42):
            self.assertIsNone(planner._maybe_report(hours, {'now_utc': 'synthetic'}))
        self.assertEqual(planner._maybe_report(48, {'now_utc': 'synthetic'})['action'], 'report')
        self.assertEqual(planner.reports, 1)

    def test_original_v2_backend_preserves_veto_semantics(self):
        planner = self.make_fault_planner(original=True)
        for hours in (0, 6, 12, 36, 42, 48):
            self.assertIsNone(planner._maybe_report(hours, {}))
        self.assertEqual(planner.reports, 0)

    def test_healthy_evidence_clears_veto_and_never_reports(self):
        planner = self.make_fault_planner()
        planner._fault_vetoes = 1
        planner.state.fault_evidence = lambda: SimpleNamespace(drop=0.9)
        self.assertIsNone(planner._maybe_report(50, {}))
        self.assertEqual(planner._fault_vetoes, 0)

    def test_default_preserves_original_numerical_search_without_review(self):
        from test_real_state import public_input, SurveyState
        init, _, _ = public_input('b')
        with patch.dict(os.environ, {'AGENT_MODEL_BACKEND': 'pi-fixed', 'OPENAI_API_KEY': '', 'KIMI_API_KEY': ''}):
            planner = Planner(SurveyState(init))
        self.assertIsNone(planner.plan_reviewer)
        self.assertIsInstance(planner.advisor, EventAdvisor)
        planner.close()


if __name__ == '__main__':
    unittest.main()
