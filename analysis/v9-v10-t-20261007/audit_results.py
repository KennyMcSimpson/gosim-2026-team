"""Read result ZIPs without executing their contents or exporting private payloads."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
from zipfile import ZipFile


def keep(values, names):
    return {name: values.get(name) for name in names}


def audit(path, portable=False):
    with ZipFile(path) as bundle:
        names = bundle.namelist()
        evaluation = json.loads(bundle.read("evaluation.json"))
        cards = {}
        for member in sorted(name for name in names if name.endswith("/agent.log")):
            card = member.split("/", 1)[0]
            lines = bundle.read(member).decode("utf-8", "replace").splitlines()
            audit_line = next(line.split(" audit ", 1)[1] for line in reversed(lines)
                              if " audit {" in line)
            metrics = json.loads(audit_line)
            model = metrics["model"]
            operations = metrics.get("operations", {})
            proposal = metrics.get("strategy_proposal", {}).get("proposal", {})
            forecast = metrics.get("adaptive_forecast", {})
            effects = metrics.get("model_effects", [])
            score = json.loads(bundle.read(f"{card}/score_report.json"))
            workflow = json.loads(bundle.read(f"{card}/workflow_result.json"))
            settled = workflow["score_report"]
            if any(score.get(key) != settled.get(key) for key in
                   ("total", "components", "counts", "termination", "uniformity", "class")):
                raise ValueError(f"settlement mismatch: {card}")
            if abs(score["total"] - sum(score["components"].values())) > 0.000002:
                raise ValueError(f"component total mismatch: {card}")
            changes = Counter()
            for effect in effects:
                before, after = effect.get("before", {}), effect.get("after", {})
                changes.update(key for key in ("program", "duration_seconds", "pointing", "action")
                               if before.get(key) != after.get(key))
            stages = {name: keep(values, ("attempts", "success", "failure", "timeout", "retries",
                                         "rejected", "total_tokens", "usage_responses"))
                      for name, values in model.get("by_stage", {}).items()}
            cards[card] = {
                "total": score["total"],
                "components": score["components"],
                "counts": score["counts"],
                "termination": score["termination"],
                "scenario_sha256": score.get("sha256", {}).get("scenario"),
                "settlement_matches_workflow": True,
                "clock": keep(workflow.get("fair_clock", {}), (
                    "charged_seconds", "run_wall_seconds", "hard_cap_reached")),
                "agent_version": metrics.get("agent_version"),
                "model": keep(model, ("attempts", "success", "failure", "timeout", "retries",
                                      "http_statuses", "provider_errors", "last_http_error",
                                      "active_http_attempts")),
                "interface": keep(model.get("interface", {}), ("host", "model", "endpoint", "protocol")),
                "stage": stages,
                "operations": keep(operations, (
                    "notes_discovered", "notes_succeeded", "notes_failed", "note_submissions",
                    "ledger_events", "provider_deferred_segments")),
                "proposal": keep(proposal, ("submissions", "stale_replies", "changes", "selections",
                                            "fallbacks", "transport_errors", "malformed_profiles")),
                "forecast_metrics": keep(forecast.get("metrics", {}), (
                    "submissions", "stale_replies", "accepted_packets", "transport_errors", "invalid_packets")),
                "weather_groups": [{"trusted": row.get("trusted"),
                                    "mixture_weight": row.get("mixture_weight"), "support": row.get("support")}
                                   for row in forecast.get("weather", {}).get("groups", [])],
                "actions": {name: value for name, value in metrics.get("actions", {}).items()
                            if isinstance(value, (int, float))},
                "effect_events": len(effects),
                "effect_log_events": sum("v10 model_effect {" in line for line in lines),
                "effect_feedback_events": sum(bool(effect.get("feedback")) for effect in effects),
                "effect_changed_fields": dict(changes),
                "runtime_fingerprint_present": any("runtime_sha256" in line for line in lines),
            }
            if portable:
                cards[card]["model"].pop("last_http_error", None)
                cards[card]["model"].pop("active_http_attempts", None)
        ordinary = [row for name, row in cards.items() if int(name.split("-", 1)[0]) <= 4]
        adverse = [row for name, row in cards.items() if int(name.split("-", 1)[0]) > 4]
        statuses, errors = Counter(), Counter()
        for row in cards.values():
            statuses.update(row["model"].get("http_statuses") or {})
            errors.update(row["model"].get("provider_errors") or {})
        return {
            "source": path.name if portable else str(path),
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "evaluation": keep(evaluation, ("evaluation_id", "revision_id", "version", "created_at",
                                            "model_provided", "model_disabled")),
            "cards": cards,
            "summary": {
                "card_count": len(cards), "d1_present": any("d1" in name for name in cards),
                "http_attempts": sum(row["model"]["attempts"] for row in cards.values()),
                "successful_completed_calls": sum(row["model"]["success"] for row in cards.values()),
                "terminal_failed_calls": sum(row["model"]["failure"] for row in cards.values()),
                "http_statuses": dict(statuses), "provider_errors": dict(errors),
                "ordinary_success": sum(row["model"]["success"] for row in ordinary),
                "adverse_success": sum(row["model"]["success"] for row in adverse),
                "a_to_d_mean": statistics.mean(row["total"] for row in ordinary),
                "a1_to_c1_mean": statistics.mean(row["total"] for row in adverse),
                "model_effect_events": sum(row["effect_events"] for row in cards.values()),
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--portable", action="store_true",
                        help="Use archive basenames and omit last-error/active-request details.")
    args = parser.parse_args()
    bundles = {path.name: audit(path, portable=args.portable) for path in args.archive}
    payload = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "bundles": bundles,
               "boundary": {"archived_results_only": True, "real_model_requests_this_audit": 0,
                            "runtime_source_binding_verified": False, "causal_score_gain_verified": False,
                            "requests_include_retries": True, "success_failure_are_completed_call_counts": True}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({name: data["summary"] for name, data in bundles.items()}, indent=2))


if __name__ == "__main__":
    main()
