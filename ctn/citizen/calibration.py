"""Estimate a local noise threshold from caller-designated null-post runs.

Does not validate that posts are truly null, or establish prediction accuracy.
"""

from dataclasses import replace
import math

from .domain import AlarmPolicy, CitizenError, fingerprint, number, text


def calibrate_null_runs(store, run_ids, *, reference, quantile=.99):
    text(reference)
    number(quantile, .5, 1)
    if type(run_ids) not in {list, tuple} or len(set(run_ids)) < 20 or len(set(run_ids)) != len(run_ids):
        raise CitizenError("calibration_requires_twenty_unique_runs")
    runs = [store.run(run_id) for run_id in run_ids]
    if (any(run["status"] != "completed" or not run["result"] or
            run["result"].get("scope") != "synthetic_panel_not_population_or_official_score" for run in runs) or
            len({run["version_id"] for run in runs}) != len(runs) or
            len({run["config"]["calibration_scope"] for run in runs}) != 1):
        raise CitizenError("incompatible_calibration_runs")
    maxima = sorted(max(abs(delta) for profile in run["result"]["profiles"]
                        for delta in profile["deltas"]["public_access"]) for run in runs)
    threshold = max(.000001, maxima[max(0, math.ceil(quantile * len(maxima)) - 1)])
    base = AlarmPolicy(**runs[0]["config"]["policy"])
    policy = replace(base, mode="shadow", delta_threshold=threshold,
                     calibration_reference=reference + ":" + fingerprint(sorted(run_ids)),
                     calibration_scope=runs[0]["config"]["calibration_scope"])
    report = {"scope": "local_null_noise_only_not_NPC_accuracy", "run_ids": list(run_ids),
              "sample_count": len(runs), "quantile": quantile, "threshold": threshold,
              "null_labels": "caller_designated_not_automatically_verified", "policy": policy.to_dict()}
    return policy, report
