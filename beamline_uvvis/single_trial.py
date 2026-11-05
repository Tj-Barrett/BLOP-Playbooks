"""
Halide Perovskite — Single Trial Pre-Flight Check

Submits one acquisition trial through the full queue-server stack to verify
the end-to-end path (pumps → QEPro → evaluation) before a full campaign.

⚠️  Physical safety — read before running
    Disconnect the DDS and Ultra syringes from their drive screws before
    running this script.  The pump motors will receive a move command and
    the screws will turn.  Without disconnection you will dispense fluid.

Prerequisites
-------------
1. validate_paths.ipynb ran cleanly — all devices connected.
2. Queue-server worker running with the halide profile.
3. A saved campaign config JSON (produced by halide_beamline.py or
   halide_beamline_updated.ipynb).

Usage
-----
    XPD_HALIDE_CONFIG=configs/halide_beamline.json python single_trial.py
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config path
# ---------------------------------------------------------------------------

env_keys = [
    "QSERVER_HTTP_URI",
    "QSERVER_HTTP_SERVER_API_KEY",
    "ZMQ_CONSUMER_ADDR",
    "TILED_URI",
    "XPD_HALIDE_CONFIG",
]

print("Environment variables:")
print("-" * 50)
for key in env_keys:
    val = os.environ.get(key, "<not set>")
    if "KEY" in key and val != "<not set>":
        val = val[:6] + "..." if len(val) > 6 else "***"
    print(f"  {key:<35} {val}")

CONFIG_PATH: str = os.environ.get("XPD_HALIDE_CONFIG", "configs/halide_beamline.json")
print()
print(f"  Config path  : {CONFIG_PATH}")
print(f"  Config exists: {Path(CONFIG_PATH).exists()}")

# ---------------------------------------------------------------------------
# Load campaign config
# ---------------------------------------------------------------------------

from xpd_tools.optimization.campaign import load_campaign_config  # noqa: E402

build_agent = load_campaign_config(CONFIG_PATH)

print("\nBuildAgent reconstructed:")
print(f"  evaluation_method : {build_agent.evaluation_method}")
print(f"  DOFs              : {[p.id for p in build_agent.pumps]}")
print(f"  rate_unit           : {build_agent.rate_unit}")
print(f"  mixer_lengths_cm    : {build_agent.mixer_lengths_cm}")
print(f"  residence_time_ratio: {build_agent.residence_time_ratio}")
print(f"  tubing_id_mm        : {build_agent.tubing_id_mm}")

# ---------------------------------------------------------------------------
# Build agent and verify plan registration
# ---------------------------------------------------------------------------

from xpd_tools.optimization.campaign import assert_plan_registered  # noqa: E402

run_agent = build_agent.build()

print("\nQueueserverAgent built.")
print(f"  acquisition_plan : {run_agent.acquisition_plan}")
print(f"  actuators        : {list(run_agent.actuators)}")

assert_plan_registered(run_agent, plan_name="uvvis_acquire")

# ---------------------------------------------------------------------------
# Trial point — adjust rates (ul/min) to match your reagent setup
# ---------------------------------------------------------------------------

single_trial_point: list[dict[str, float]] = [{
    "infusion_rate_CsPb":  40.0,   # dds2_p1 — CsPb precursor
    "infusion_rate_TOABr": 30.0,   # dds2_p2 — TOABr
    "infusion_rate_Cl":    10.0,   # dds3_p1 — Pb-rich / TOACl
    "infusion_rate_I":     20.0,   # dds3_p2 — TOAI
}]

print("\nTrial point:")
for k, v in single_trial_point[0].items():
    print(f"  {k:<25} {v} ul/min")

# ---------------------------------------------------------------------------
# Submit and block until done
# ---------------------------------------------------------------------------

print("\nSubmitting trial to queue server...")
future = run_agent.submit_suggestions(single_trial_point)

print("Waiting for acquisition and evaluation to complete...")
result = future.result()

print("\nResult:")
print(result)
