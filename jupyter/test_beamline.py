#!/usr/bin/env python
"""Beamline smoke test for xpd-tools blop-agent (queue-server path).

Run at the beamline BEFORE a campaign. Each stage isolates one failure mode.
First stage to FAIL tells you exactly what is broken.

Usage::

    export XPD_HALIDE_CONFIG=/path/to/halide_beamline.json
    export XPD_HTTP_API_KEY=<queue-server key>
    python test_beamline.py

Stages
------
1. Env vars present + config loads
2. Queue-server HTTP reachable + authenticated   (401 surfaces here)
3. Tiled read client resolves
4. BuildAgent.from_config(...).build()           (QueueserverAgent construct)
5. Plan 'uvvis_acquire' registered on worker
"""

from __future__ import annotations

import json
import os
import sys
import traceback

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
RESET  = "\033[0m"

def _ok(msg):   print(f"  {GREEN}[PASS]{RESET} {msg}")
def _bad(msg):  print(f"  {RED}[FAIL]{RESET} {msg}")
def _warn(msg): print(f"  {YELLOW}[WARN]{RESET} {msg}")
def _info(msg): print(f"  [info] {msg}")

def stage(n: int, title: str) -> None:
    print(f"\n{'='*64}\nStage {n}: {title}\n{'='*64}")


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def stage1_env(cfg_out: list) -> int:
    """Check env vars, load config, validate queue_server flag."""
    stage(1, "Environment variables + config")

    config_path = os.environ.get("XPD_HALIDE_CONFIG", "")
    http_key    = os.environ.get("XPD_HTTP_API_KEY", "")

    if not config_path:
        _bad("XPD_HALIDE_CONFIG not set — point it at the beamline config JSON")
        return 1
    _ok(f"XPD_HALIDE_CONFIG = {config_path}")

    if not os.path.exists(config_path):
        _bad(f"config file not found: {config_path}")
        return 1
    _ok("config file exists")

    cfg = json.load(open(config_path))
    cfg_out.append(cfg)

    conn = cfg["connection"]
    _info(f"queue_server    = {conn['queue_server']}")
    _info(f"http_server_uri = {conn['http_server_uri']}")
    _info(f"tiled_profile   = {conn['tiled_profile']}")
    _info(f"tiled_uri       = {conn['tiled_uri']}")

    if not conn["queue_server"]:
        _bad("config has queue_server=false — this script tests the queue-server path")
        return 1
    _ok("queue_server=true")

    if not http_key:
        _bad(
            "XPD_HTTP_API_KEY is empty — queue server will reject with 401.\n"
            "       Export the key before running:\n"
            "         export XPD_HTTP_API_KEY=<key>"
        )
        return 1
    _ok(f"XPD_HTTP_API_KEY set (len={len(http_key)})")

    return 0


def stage2_qserver(conn: dict, http_key: str, api_out: list) -> int:
    """Raw authenticated status call — surfaces 401 before full agent build."""
    stage(2, "Queue-server HTTP: reachable + authenticated")

    try:
        from bluesky_queueserver_api.http import REManagerAPI

        api = REManagerAPI(http_server_uri=conn["http_server_uri"])
        api.set_authorization_key(api_key=http_key)
        status = api.status()
        api_out.append(api)
    except Exception as exc:
        _bad(f"queue-server status call failed: {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg or "permission" in msg:
            _info("→ 401: key missing, invalid, or wrong scope.")
            _info("  Confirm with:")
            _info(
                f'    curl -s -H "Authorization: ApiKey $XPD_HTTP_API_KEY" '
                f'{conn["http_server_uri"]}/api/status'
            )
            _info("  If curl also 401s, ask beamline staff to issue a valid key.")
        elif "ssl" in msg or "certificate" in msg:
            _info("→ TLS error: httpserver cert expired or self-signed.")
            _info("  Check whether the server is on HTTP (not HTTPS) locally:")
            _info("    http://localhost:60610 vs https://xf28id2-xpd-qs1.nsls2.bnl.gov")
        else:
            _info("→ Connection error: is start-re-manager + httpserver running?")
            _info("  Start with:")
            _info("    start-re-manager --startup-module "
                  "xpd_tools.optimization.profiles.halide &")
            _info("    uvicorn --host localhost --port 60610 "
                  "bluesky_httpserver.server:app &")
        return 1

    _ok("authenticated — /status returned")
    _info(f"manager_state          = {status.get('manager_state')}")
    _info(f"worker_environment_up  = {status.get('worker_environment_exists')}")
    _info(f"items_in_queue         = {status.get('items_in_queue')}")
    _info(f"re_state               = {status.get('re_state')}")

    if not status.get("worker_environment_exists"):
        _warn(
            "Worker environment NOT open — plan list in Stage 5 will be empty.\n"
            "       Open it with:  api.environment_open()\n"
            "       Or via CLI:    qserver environment open"
        )

    return 0


def stage3_tiled(conn: dict) -> int:
    """Tiled read client resolution — non-fatal, warns for later evaluation."""
    stage(3, "Tiled read client resolves")

    try:
        from tiled.client import from_profile, from_uri

        if conn["tiled_uri"]:
            tc = from_uri(conn["tiled_uri"])
        else:
            tc = from_profile(conn["tiled_profile"])
        _ok(f"Tiled client resolved: {type(tc).__name__}")

        try:
            keys = list(tc)[:5]
            _info(f"top-level keys (first 5): {keys}")
        except Exception as e:
            _info(f"listing entries failed (non-fatal): {e}")

    except Exception as exc:
        # Non-fatal — build() doesn't need Tiled; evaluator does (later)
        _warn(f"Tiled resolve failed (non-fatal at build time): {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            _info("→ Tiled 401: profile has no / expired token.")
            _info("  Check ~/.config/tiled/profiles/*.yml")
        elif "profile" in msg:
            _info(f"→ profile '{conn['tiled_profile']}' not in "
                  "~/.config/tiled/profiles/")
        _info("  Tiled reads happen at evaluation time, not build time.")
        _info("  Build will proceed; fix Tiled auth before the first trial.")

    return 0  # always continue


def stage4_build(cfg: dict, http_key: str, run_agent_out: list) -> int:
    """BuildAgent.from_config().build() — the QueueserverAgent constructor."""
    stage(4, "BuildAgent.from_config(...).build()")

    try:
        from xpd_tools.optimization.agent import BuildAgent

        build_agent = BuildAgent.from_config(cfg, http_api_key=http_key)
        _ok("BuildAgent.from_config() OK")
        _info(f"http_server_uri = {build_agent.http_server_uri}")
        _info(f"http_api_key    = {'set' if build_agent.http_api_key else 'MISSING'}")
        _info(f"tiled_profile   = {build_agent.tiled_profile}")
    except Exception as exc:
        _bad(f"from_config() failed: {exc}")
        traceback.print_exc()
        return 1

    try:
        run_agent = build_agent.build()
        run_agent_out.append(run_agent)
        _ok("build() OK — QueueserverAgent constructed")
    except Exception as exc:
        _bad(f"build() failed: {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            _info("→ 401 inside build(): http_api_key not reaching REManagerAPI.")
            _info("  Check that XPD_HTTP_API_KEY is non-empty and correct.")
        traceback.print_exc()
        return 1

    return 0


def stage5_plans(run_agent: object) -> int:
    """Confirm uvvis_acquire is registered on the worker."""
    stage(5, "Plan 'uvvis_acquire' registered on worker")

    try:
        from xpd_tools.optimization.campaign import assert_plan_registered

        assert_plan_registered(run_agent, "uvvis_acquire")
        _ok("uvvis_acquire registered and ready")
    except RuntimeError as exc:
        _bad(str(exc))
        _info("Worker must start with:")
        _info("  start-re-manager --startup-module "
              "xpd_tools.optimization.profiles.halide")
        _info("and XPD_HALIDE_CONFIG set in the worker environment.")
        return 1
    except Exception as exc:
        _bad(f"plan check failed: {exc}")
        traceback.print_exc()
        return 1

    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("xpd-tools beamline smoke test (queue-server path)")
    print(f"python {sys.version.split()[0]}  |  pid {os.getpid()}")

    cfg_out      = []
    api_out      = []
    run_agent_out = []

    # Stage 1
    if stage1_env(cfg_out):
        return 1

    cfg  = cfg_out[0]
    conn = cfg["connection"]
    http_key = os.environ["XPD_HTTP_API_KEY"]

    # Stage 2
    if stage2_qserver(conn, http_key, api_out):
        return 1

    # Stage 3 (non-fatal)
    stage3_tiled(conn)

    # Stage 4
    if stage4_build(cfg, http_key, run_agent_out):
        return 1

    # Stage 5
    if stage5_plans(run_agent_out[0]):
        return 1

    print(f"\n{'='*64}")
    print(f"{GREEN}ALL STAGES PASSED — queue-server path ready.{RESET}")
    print(f"{'='*64}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
