"""
Halide Perovskite UV-Vis Optimization — Campaign Script

Equivalent to halide_beamline_updated.ipynb; use this form at the beamline
when running headlessly or when Jupyter is not available.

Usage
-----
Build a fresh config and run a campaign::

    python halide_beamline.py

Reload a prior config::

    RELOAD_CONFIG=1 XPD_HALIDE_CONFIG=configs/halide_beamline.json \\
        python halide_beamline.py

Environment variables (set in shell or .env):
    QSERVER_HTTP_URI             queue-server HTTP endpoint
    QSERVER_HTTP_SERVER_API_KEY  API key for the queue server
    ZMQ_CONSUMER_ADDR            ZMQ document-dispatcher address
    TILED_URI                    Tiled URI for raw/evaluated data
    TILED_PROFILE                Tiled profile name (default: xpd)
    RELOAD_CONFIG                Set to "1" to reload instead of building fresh
    RELOAD_CONFIG_PATH           Path to JSON config to reload (default: configs/halide_beamline.json)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------

HTTP_SERVER_URI: str = os.environ.get(
    "QSERVER_HTTP_URI",
    "https://xf28id2-xpd-qs1.nsls2.bnl.gov",
)
HTTP_API_KEY: str = os.environ.get("QSERVER_HTTP_SERVER_API_KEY", "")
ZMQ_CONSUMER_ADDR: str = os.environ.get(
    "ZMQ_CONSUMER_ADDR",
    "ipc:///var/lib/bluesky-zmq-proxy/xpd-ipc-in-ipc-out/out.sock",
)
TILED_URI: str = os.environ.get("TILED_URI", "https://tiled.nsls2.bnl.gov")
TILED_PROFILE: str = os.environ.get("TILED_PROFILE", "xpd")

AGENT_DATA_PATH: str | None = "data/halide_data_uvvis_only.csv"

RELOAD_CONFIG: bool = os.environ.get("RELOAD_CONFIG", "0").strip() in ("1", "true", "yes")
RELOAD_CONFIG_PATH: str = os.environ.get("RELOAD_CONFIG_PATH", "configs/halide_beamline.json")

print("HTTP_SERVER_URI  :", HTTP_SERVER_URI)
print("ZMQ_CONSUMER_ADDR:", ZMQ_CONSUMER_ADDR)
print("TILED_URI        :", TILED_URI)
print("Mode             :", "RELOAD" if RELOAD_CONFIG else "BUILD FRESH")

# ---------------------------------------------------------------------------
# Build or reload the BuildAgent
# ---------------------------------------------------------------------------

from xpd_tools.optimization.agent import BuildAgent  # noqa: E402
from xpd_tools.optimization.campaign import (  # noqa: E402
    assert_plan_registered,
    load_campaign_config,
    save_campaign_config,
)

if RELOAD_CONFIG:
    build_agent = load_campaign_config(RELOAD_CONFIG_PATH)
    print(f"\nReloaded config from {RELOAD_CONFIG_PATH}")
    print("  evaluation_method :", build_agent.evaluation_method)
    print("  DOFs              :", [p.id for p in build_agent.pumps])
    print("  run.iterations    :", build_agent.run_settings.iterations)

else:
    from xpd_tools.optimization.helpers.dofs import Pump  # noqa: E402
    from xpd_tools.optimization.helpers.qepro import (  # noqa: E402
        PlqyReference,
        QualityPolicy,
        SpectraFitSettings,
    )
    from xpd_tools.optimization.plans import DilutionStage, FlowSource, WashCycle  # noqa: E402

    build_agent = BuildAgent(
        queue_server=True,
        agent_data_path=AGENT_DATA_PATH,
        http_server_uri=HTTP_SERVER_URI,
        http_api_key=HTTP_API_KEY,
        zmq_consumer_address=ZMQ_CONSUMER_ADDR,
        tiled_profile=TILED_PROFILE,
        evaluation_method="uvvis",
    )

    build_agent.set_metadata({
        "beamline": "28id2",
        "tags": ["qserver", "bluesky", "uvvis"],
        "comment": "Halide Perovskite UV-Vis Optimization",
    })

    # -- UV-Vis objectives ------------------------------------------------
    PEAK_TARGET: float = 520   # nm
    PEAK_TOLERANCE: float = 5  # nm

    build_agent.set_uvvis_objectives(
        peak_target=PEAK_TARGET,
        peak_tolerance=PEAK_TOLERANCE,
        fwhm_role="objective",
        plqy_role="objective",
        peak_distance_role="objective",
        max_peak_distance_nm=PEAK_TOLERANCE * 10,
        min_plqy=1e-3,
        max_fwhm_nm=60,
        max_retries=10,
        retry_delay=2.0,
        fit_settings=SpectraFitSettings(
            pl_screen_key_height=200,
            pl_screen_peak_height=50,
            pl_screen_peak_distance=100,
            pl_percent_range=(40, 100),
            pl_wavelength_range=(400, 800),
            pl_fit_maxfev=100000,
            pl_fit_r2_window_sigma=3,
            absorbance_percent_range=(10, 70),
            absorbance_wavelength_range=(210, 700),
        ),
        plqy=PlqyReference(
            excitation_wavelength_nm=365,
            absorbance=0.361,
            pl_integral=952628,
            refractive_index=1.337,
            plqy=0.546,
            solvent_refractive_index=1.506,
        ),
    )

    # -- DOFs (pump bounds in ul/min) -------------------------------------
    _dds2_p1 = "CsPb"
    _dds2_p2 = "TOABr"
    _dds3_p1 = "Cl"
    _dds3_p2 = "I"

    build_agent.set_dofs([
        Pump(name=_dds2_p1, id="dds2_p1", bounds=(0, 100)),   # CsPb precursor
        Pump(name=_dds2_p2, id="dds2_p2", bounds=(0, 100)),   # TOABr
        Pump(name=_dds3_p1, id="dds3_p1", bounds=(0, 100)),   # TOACl / Pb-rich
        Pump(name=_dds3_p2, id="dds3_p2", bounds=(0, 100)),   # TOAI
    ])

    # -- Experiment (sources, dilutions, wash + flow geometry) ------------
    build_agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump="dds2_p1",
                precursor=_dds2_p1,
                sample_label="Cs-rich 33mM",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_TOABr",
                pump="dds2_p2",
                precursor=_dds2_p2,
                sample_label="TOABr",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_Cl",
                pump="dds3_p1",
                precursor=_dds3_p1,
                sample_label="Pb-rich 33mM",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_I",
                pump="dds3_p2",
                precursor=_dds3_p2,
                sample_label="I",
                loaded_ml=30.0,
            ),
        ],
        dilutions=[
            DilutionStage(
                pump="dds1_p1",
                ratio=1.0,
                position="before_equilibrium",
                syringe_ml=20,
                material="plastic_BD",
                target_ml=20,
                loaded_ml=20.0,
            ),
            DilutionStage(
                pump="ultra2",
                ratio=1.0,
                position="after_equilibrium",
                syringe_ml=100,
                material="steel",
                target_ml=100,
                loaded_ml=100.0,
                wait_sec=30,
            ),
        ],
        wash_cycles=[
            WashCycle(pump="ultra1", loaded_ml=30.0),
        ],
        # Flow geometry — explicit so the values are visible here and
        # propagated into flow_config when build() submits to queueserver.
        rate_unit="ul/min",
        mixer_lengths_cm=(30.0,),
        residence_time_ratio=1.0,
    )

    # -- Quality policy ---------------------------------------------------
    build_agent.quality_policy = QualityPolicy(
        enabled=True,
        good_batches=3,
        max_bad_batches=3,
        absorbance_shots=10,
        fluorescence_shots=10,
    )

    # -- Run settings -----------------------------------------------------
    build_agent.set_run(
        iterations=50,
        extra_initialization_trials=5,
        generation_strategy={
            "initialize_with_center": False,
            "min_observed_initialization_trials": 2,
            "allow_exceeding_initialization_budget": True,
        },
    )

    cfg_path = save_campaign_config(build_agent, "halide_beamline")
    print(f"\nConfig archived → {cfg_path}")

# ---------------------------------------------------------------------------
# Build the QueueserverAgent and verify plan registration
# ---------------------------------------------------------------------------

run_agent = build_agent.build()

print("\nQueueserverAgent built.")
print("  acquisition_plan :", run_agent.acquisition_plan)
print("  actuators        :", list(run_agent.actuators))

assert_plan_registered(run_agent, plan_name="uvvis_acquire")

# ---------------------------------------------------------------------------
# Seed historical data into the generation strategy
# ---------------------------------------------------------------------------

n_historical = build_agent.configure_generation_strategy(run_agent)
print(f"\n{n_historical} historical trials ingested into the generation strategy.")

# ---------------------------------------------------------------------------
# Optimization loop
# ---------------------------------------------------------------------------

campaign_future = run_agent.run(
    iterations=build_agent.run_settings.iterations,
    n_points=build_agent.run_settings.n_points,
    checkpoint_interval=10,
)

print(f"\nCampaign started — {build_agent.run_settings.iterations} iterations.")
print("Watching progress (Ctrl-C or run_agent.stop() to halt early)...\n")

while not campaign_future.done():
    print(
        f"  iteration {run_agent.current_iteration}"
        f" / {build_agent.run_settings.iterations}"
    )
    time.sleep(30)

campaign_result = campaign_future.result()
print("\nCampaign complete.")
print(campaign_result)
