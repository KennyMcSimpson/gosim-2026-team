"""Small, deterministic v6 regression checks.

These checks use only synthetic public-protocol-shaped payloads.  They are
evidence for the four policy changes, not a substitute for a hosted score.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

V5 = Path(__file__).resolve().parents[1]
PROJECT = V5 / "project"
sys.path.insert(0, str(PROJECT))

from agent_core.advisor import EventAdvisor  # noqa: E402
from agent_core.planner import Planner  # noqa: E402
from agent_core.state import SurveyState  # noqa: E402
from agent_core.geometry import local_sidereal_deg  # noqa: E402


UTC = timezone.utc


def init_payload(*, required=False, weight=1.0, target_id="T0", second=None):
    start = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
    end = start + timedelta(hours=8)
    site = {"latitude_deg": 0.0, "longitude_deg": 0.0,
            "minimum_altitude_deg": 30.0}
    # Put the fixture about 20 degrees off the meridian at the test instant;
    # a target exactly at zenith is rejected by the official pointing guard.
    ra = local_sidereal_deg(start + timedelta(minutes=35), 0.0) - 20.0
    rows = [[target_id, ra, 0.0, 0.5, weight, required]]
    if second is not None:
        rows.append([second, ra, 0.0, 0.5, 0.0, False])
    return {
        "site": site,
        "survey": {"start_utc": start.isoformat(), "end_utc": end.isoformat(),
                   "nights": [{"observing_start_utc": start.isoformat(),
                               "observing_end_utc": end.isoformat()}]},
        "instrument": {"grid_side": 1, "n_fibers": 1,
                        "glass_side_deg": 1.0, "pitch_deg": 1.0,
                        "fov_side_deg": 1.0,
                        "exposure": {"min_duration_seconds": 60,
                                     "max_duration_seconds": 3600}},
        "scoring": {"q0": 0.68, "flux_zero_point": 0.5,
                    "exposure_zero_point_seconds": 900,
                    "airmass_exponent": 0.6,
                    "required": {"penalty_per_missing": 50,
                                  "observed_factor_threshold": 0.5},
                    "observation_requests": {"completion_factor_threshold": 0.5}},
        "targets": {"columns": ["target_id", "ra_deg", "dec_deg",
                                  "feature_flux", "science_weight", "required"],
                    "rows": rows},
    }


class Trace:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(dict(event))


class FakeClient:
    calls_made = 0
    max_calls = 64

    def ask_json(self, _prompt, _context, _wall_left):
        self.calls_made += 1
        return {"avoid_directions": ["S"], "confidence": 0.95}


def llm_evidence_check():
    state = SurveyState(init_payload())
    trace = Trace()
    advisor = EventAdvisor(FakeClient(), trace, lambda _text: None)
    # Keep this test synchronous: the production path submits the same record
    # to a background worker, while the application contract is tested here.
    advisor._submit = lambda *_args, **_kwargs: None
    selected = [{"event_kind": "rocket_launch", "direction": "S",
                 "text": "local south sector"}]
    context = {"notices": selected}
    advisor.answers.put(("notice_interpretation", 0,
                         {"answer": {"avoid_directions": ["S"],
                                     "confidence": 0.95},
                          "failure_reason": None,
                          "client_outcome": {"status": "success", "attempts": 1},
                          "latency_seconds": 0.001},
                         context, "notice_interpretation:0:1:test"))
    payload = {"latest_bulletin": {"notices": selected},
               "active_requests": []}
    advisor.update(state, payload, 0, 1000.0, [])
    applied = next(e for e in trace.events if e.get("event") == "llm_applied")
    assert state.extra_avoid == {"S"}
    assert applied["succeeded"] and applied["parsed"] and applied["applied"]
    assert applied["changed"] and applied["state_before"] == []
    assert applied["state_after"] == ["S"]
    assert applied["decision_consumer"] == "Planner._direction_factor"

    # A provider failure is explicit and cannot mutate state.
    advisor.answers.put(("notice_interpretation", 0,
                         {"answer": None, "failure_reason": "timeout",
                          "client_outcome": {"status": "fallback", "attempts": 2},
                          "latency_seconds": 0.002},
                         context, "notice_interpretation:0:2:fail"))
    advisor.update(state, payload, 0, 1000.0, [])
    fallback = [e for e in trace.events if e.get("event") == "llm_fallback"][-1]
    assert fallback["reason"] == "timeout"
    return {
        "state_before": [], "state_after": ["S"],
        "applied": applied, "fallback": fallback,
    }


def resync_check():
    payload = init_payload(required=True, target_id="REQ_REQUIRED", second="REQ_REQUEST")
    state = SurveyState(payload)
    message = {
        "record_type": "state_resync",
        "trigger_event_id": "V4EV0015",
        "invalidated_window": {"action_index_start": 2,
                                "action_index_end_exclusive": 3},
        "best_scores": [{"target_id": "REQ_REQUIRED", "best_score": 0.0},
                        {"target_id": "REQ_REQUEST", "best_score": 0.0}],
        "observation_requests": [{
            "request_id": "RQ001", "remaining_count": 1,
            "target_ids": ["REQ_REQUEST"], "completed_target_ids": [],
        }],
    }
    state.on_messages([message], None)
    ids = sorted(state.ids[i] for i in state.recovery_target_indices())
    assert ids == ["REQ_REQUEST", "REQ_REQUIRED"]
    assert state.last_resync["event_id"] == "V4EV0015"
    return {"event_id": state.last_resync["event_id"],
            "recovery_target_ids": state.last_resync["recovery_target_ids"],
            "request_ids": state.last_resync["request_ids"]}


def search_check():
    now = datetime(2026, 11, 2, 0, 35, tzinfo=UTC)
    end = now + timedelta(hours=7)

    state = SurveyState(init_payload(weight=100.0, target_id="SCIENCE",
                                     second="REQ_LOW"))
    planner = Planner(state, log=lambda _text: None)
    planner._request_views_now = [{"id": "RQ001", "issued": now - timedelta(minutes=1),
                                   "deadline": now + timedelta(hours=1),
                                   "needed": {1}, "remaining": 1,
                                   "threshold": 0.5, "reward": 1000.0}]
    planner._request_thresholds_now = {1: 0.5}
    planner._request_bonus_now = {1: 1000.0}
    planner._search_allowance = 2.0
    planner._search_deadline = 10**12
    planner.required_calendar.builds_left = 8
    plans, _best_science = planner._search(now, end, 0, 0.1)
    request_seen = any(any(item["i"] == 1 for item in p["items"].values())
                       for p in plans)
    assert request_seen

    state2 = SurveyState(init_payload(weight=100.0, target_id="SCIENCE",
                                       second="REQ_LOW"))
    state2.required[1] = True
    planner2 = Planner(state2, log=lambda _text: None)
    planner2._request_views_now = []
    planner2._request_thresholds_now = {}
    planner2._request_bonus_now = {}
    planner2._search_allowance = 2.0
    planner2._search_deadline = 10**12
    planner2.required_calendar.builds_left = 8
    plans2, _best_science = planner2._search(now, end, 0, 0.1)
    required_seen = any(any(item["i"] == 1 for item in p["items"].values())
                        for p in plans2)
    assert required_seen
    return {"request_target_seen": request_seen,
            "required_target_seen": required_seen,
            "request_plans": len(plans), "required_plans": len(plans2)}


def required_median_check():
    payload = init_payload(required=True, weight=0.0, target_id="REQ_MEDIAN")
    state = SurveyState(payload)
    planner = Planner(state, log=lambda _text: None)
    item = {"i": 0, "ks": (0.0, 0.0, 0.0), "band_qs": (0.0, 0.0, 0.0),
            "direction": 1.0, "low_k": 0.0004, "med_k": 0.0006,
            "valid": 1.0, "required_value": 10.0, "science_cache": {}}
    threshold = state.scoring.required_threshold
    duration = int(threshold / item["med_k"]) + 1
    gain = planner._target_gain(item, duration, "DARK")
    assert item["low_k"] * duration < threshold
    assert item["med_k"] * duration >= threshold
    assert gain == item["required_value"]

    planner._request_views_now = []
    planner._request_bonus_now = {}
    item["up"] = 1800
    candidates = planner._duration_candidates({0: [item]}, 1800,
                                               datetime(2026, 11, 2, 0, 35, tzinfo=UTC))
    required_duration = max(state.min_exposure,
                            int(__import__("math").ceil(threshold / item["med_k"])) + 1)
    assert required_duration in candidates
    return {"low_k_times_duration": item["low_k"] * duration,
            "med_k_times_duration": item["med_k"] * duration,
            "required_duration_candidate": required_duration,
            "required_gain": gain}


def urgency_curve_check():
    state = SurveyState(init_payload(required=True, target_id="REQ_URGENCY"))
    calendar = Planner(state, log=lambda _text: None).required_calendar
    calendar.window_nights[0] = (10,)
    penalty = max(1.0, state.scoring.required_penalty)
    values = {}
    for night_index, future in ((0, 100), (1, 6), (2, 3), (3, 1), (4, 0)):
        calendar.window_nights[0] = (night_index + future,)
        calendar.suffix[0] = [0] * (len(state.nights) + 1)
        calendar.suffix[0][min(len(state.nights), night_index + 1)] = future
        # Set all later suffix counts consistently for the requested lookup.
        for index in range(min(len(state.nights), night_index + 1) - 1, -1, -1):
            calendar.suffix[0][index] = future
        values[str(future)] = calendar.urgency(0, night_index)
    assert math.isclose(values["100"], penalty * 0.25)
    assert math.isclose(values["6"], penalty * 0.25)
    assert math.isclose(values["3"], penalty * 6.125)
    assert math.isclose(values["1"], penalty * 10.041666666666666)
    assert math.isclose(values["0"], penalty * 16.0)
    return values


def best_effort_check():
    state = SurveyState(init_payload(weight=1.0))
    planner = Planner(state, log=lambda _text: None)
    planner._prepare_uniformity = lambda: None
    planner._instrument_scale = lambda: 1.0
    planner._search = lambda *_args, **_kwargs: ([], {
        "utility": 0.0, "science": 2.0, "duration": 600, "program": "DARK",
        "items": {0: {"i": 0, "model": 1.0, "band_model": 1.0,
                      "alt": 70.0, "az": 180.0, "direction": 1.0}},
        "pointing": (70.0, 180.0), "rate": 1.0, "reward": 0.0,
        "valid": 0.8, "uniformity": 0.0, "start": datetime(2026, 11, 2, 0, 35, tzinfo=UTC),
        "completed": set(),
    })
    trace_events = []
    planner.trace.write = lambda event: trace_events.append(dict(event))
    planner.clock._started = 1.0
    planner.clock.compute_left = lambda: 100.0
    planner._search_allowance = 0.1
    planner._current_action_index = 0
    now = datetime(2026, 11, 2, 0, 35, tzinfo=UTC)
    action = planner.plan(now, now + timedelta(hours=1), 0, 0.1)
    assert action["action"] == "observe"
    assert action["decision_source"] == "best-effort"
    assert any(event.get("event") == "best_effort_fallback" for event in trace_events)
    assert any(event.get("event") == "search_summary" for event in trace_events)
    return {"action": action, "events": [e["event"] for e in trace_events]}


def duration_check():
    state = SurveyState(init_payload())
    planner = Planner(state, log=lambda _text: None)
    now = datetime(2026, 11, 2, 0, 35, tzinfo=UTC)
    planner._request_views_now = [{"id": "RQ001", "issued": now - timedelta(minutes=1),
                                   "deadline": now + timedelta(seconds=100),
                                   "needed": {0}, "remaining": 1,
                                   "threshold": 0.5, "reward": 1000.0}]
    planner._request_bonus_now = {0: 1000.0}
    item = {"i": 0, "low_k": 0.004, "med_k": 0.01, "up": 900, "required_value": 0,
            "ks": (0.01, 0.01, 0.01), "direction": 1.0}
    durations = planner._duration_candidates({0: [item]}, 900, now)
    assert 60 in durations and 100 in durations and 126 in durations
    return {"durations": durations, "shortest_request_duration": 60,
            "request_threshold_duration_from_low_k": 126}


def main():
    output = {
        "llm_evidence": llm_evidence_check(),
        "state_resync": resync_check(),
        "request_and_required_search": search_check(),
        "request_duration": duration_check(),
        "required_median": required_median_check(),
        "urgency_curve": urgency_curve_check(),
        "best_effort": best_effort_check(),
    }
    text = json.dumps(output, indent=2, sort_keys=True, default=str)
    (Path(__file__).resolve().parent / "regression_summary.json").write_text(
        text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
