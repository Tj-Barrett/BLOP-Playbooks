"""Halide Iodine X-ray + UV-Vis Optimization — Campaign Script.

Structured to match the beamline halide scripts.
"""

from __future__ import annotations

import os
import time
from dotenv import load_dotenv

from xpd_tools.optimization.agent import BuildAgent
from xpd_tools.optimization.campaign import (
    assert_plan_registered,
    load_campaign_config,
    save_campaign_config,
)
from xpd_tools.optimization.helpers.dofs import Pump
from xpd_tools.optimization.helpers.phases import Phase
from xpd_tools.optimization.helpers.qepro import PlqyReference, SpectraFitSettings
from xpd_tools.optimization.plans import DilutionStage, FlowSource, WashCycle

load_dotenv()

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

AGENT_DATA_PATH: str | None = None

RELOAD_CONFIG: bool = os.environ.get("RELOAD_CONFIG", "0").strip() in ("1", "true", "yes")
RELOAD_CONFIG_PATH: str = os.environ.get("RELOAD_CONFIG_PATH", "configs/iodine_beamline.json")

print("HTTP_SERVER_URI  :", HTTP_SERVER_URI)
print("ZMQ_CONSUMER_ADDR:", ZMQ_CONSUMER_ADDR)
print("TILED_URI        :", TILED_URI)
print("Mode             :", "RELOAD" if RELOAD_CONFIG else "BUILD FRESH")

if RELOAD_CONFIG:
    build_agent = load_campaign_config(RELOAD_CONFIG_PATH)
    print(f"\nReloaded config from {RELOAD_CONFIG_PATH}")
    print("  evaluation_method :", build_agent.evaluation_method)
    print("  DOFs              :", [p.id for p in build_agent.pumps])
    print("  run.iterations    :", build_agent.run_settings.iterations)
else:
    build_agent = BuildAgent(
        queue_server=True,
        agent_data_path=AGENT_DATA_PATH,
        http_server_uri=HTTP_SERVER_URI,
        http_api_key=HTTP_API_KEY,
        zmq_consumer_address=ZMQ_CONSUMER_ADDR,
        tiled_profile=TILED_PROFILE,
        evaluation_method="xray-uvvis",
    )

    build_agent.set_metadata(
        {
            "beamline": "28id2",
            "tags": ["qserver", "bluesky", "xray", "uvvis", "iodine"],
            "comment": "Halide Iodine X-ray + UV-Vis Optimization",
        }
    )

    build_agent.set_uvvis_objectives(
        peak_target=670,
        peak_tolerance=5,
        fwhm_role="objective",
        plqy_role="objective",
        peak_distance_role="objective",
        max_peak_distance_nm=500,
        min_plqy=1e-3,
        max_fwhm_nm=60,
        max_retries=10,
        retry_delay=2.0,
        fit_settings=SpectraFitSettings(
            pl_screen_key_height=200,
            pl_screen_peak_height=50,
            pl_screen_peak_distance=200,
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

    build_agent.set_xray_objectives(
        max_retries=10,
        retry_delay=2.0,
        exposure=6.0,
        frame_acq_time=1.5,
        no_dark=False,
        stream_name="scattering",
        screening="screen_and_record",
        use_good_bad=True,
        good_target=2,
        max_bad=3,
        num_abs=16,
        num_flu=16,
        min_radius=2.0,
        max_radius=20.0,
        objective_function="pearson",
        phases=[
            Phase(
                name="CsPbBr3",
                cif="../ref_data/CsPbBr3_Pnma_mp-567681.cif",
                gr="../ref_data/CsPbBr3_Pnma_mp-567681.gr",
                minimize=True,
                role="constraint",
                bound=1,
            ),
            Phase(
                name="CsPbCl3",
                cif="../ref_data/CsPbCl3_Pm-3m_mp-23037.cif",
                gr="../ref_data/CsPbCl3_Pm-3m_mp-23037.gr",
                minimize=True,
                role="constraint",
                bound=1,
            ),
            Phase(
                name="CsPbI3",
                cif="../ref_data/CsPbI3_Pnma_mp-540839.cif",
                gr="../ref_data/CsPbI3_Pnma_mp-540839.gr",
                minimize=False,
                role="constraint",
                bound=1,
            ),
            Phase(
                name="CsBr",
                cif="../ref_data/CsBr_Pm-3m_mp-22906.cif",
                gr="../ref_data/CsBr_Pm-3m_mp-22906.gr",
                minimize=True,
                role="constraint",
                bound=1,
            ),
            Phase(
                name="Cs4PbBr6",
                cif="../ref_data/Cs4PbBr6_R-3c_mp-23436.cif",
                gr="../ref_data/Cs4PbBr6_R-3c_mp-23436.gr",
                minimize=True,
                role="constraint",
                bound=1,
            ),
        ],
    )

    _dds2_p1 = "CsPb"
    _dds2_p2 = "TOABr"
    _dds3_p1 = "OAm_Tol"
    _dds3_p2 = "I"

    build_agent.set_dofs(
        [
            Pump(name=_dds2_p1, id="dds2_p1", bounds=(10, 200)),
            Pump(name=_dds2_p2, id="dds2_p2", bounds=(10, 200)),
            Pump(name=_dds3_p1, id="dds3_p1", bounds=(10, 200)),
            Pump(name=_dds3_p2, id="dds3_p2", bounds=(10, 200)),
        ]
    )

    build_agent.experiment(
        sources=[
            FlowSource(
                dof="infusion_rate_CsPb",
                pump="dds2_p1",
                precursor=_dds2_p1,
                sample_label="202504140CsPb(OA)3-33mM",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_TOABr",
                pump="dds2_p2",
                precursor=_dds2_p2,
                sample_label="20240912-TOABr-100mM",
                loaded_ml=30.0,
            ),
            FlowSource(
                dof="infusion_rate_OAm_Tol",
                pump="dds3_p1",
                precursor=_dds3_p1,
                sample_label="OAm_Tol",
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
            WashCycle(
                pump="ultra2",
                loaded_ml=50.0,
                rate_ul_min=200.0,
                duration_sec=300.0,
                syringe_ml=50.0,
                material="steel",
                target_ml=1.0,
                set_target=False,
                reserve_ml=1.0,
            ),
        ],
    )

    build_agent.set_run(
        iterations=100,
        extra_initialization_trials=25,
        generation_strategy={
            "initialize_with_center": False,
            "min_observed_initialization_trials": 2,
            "allow_exceeding_initialization_budget": True,
        },
    )

    cfg_path = save_campaign_config(build_agent, "iodine_beamline")
    print(f"\nConfig archived → {cfg_path}")

run_agent = build_agent.build()

print("\nQueueserverAgent built.")
print("  acquisition_plan :", run_agent.acquisition_plan)
print("  actuators        :", list(run_agent.actuators))

assert_plan_registered(run_agent, plan_name="xray_uvvis_acquire")

n_historical = build_agent.configure_generation_strategy(run_agent)
print(f"\n{n_historical} historical trials ingested into the generation strategy.")

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
