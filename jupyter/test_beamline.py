#!/usr/bin/env python
"""Beamline smoke test for NSLS2/xpd-tools @ blop-agent (queue-server path).

Targeted API (blop-agent branch ONLY):
    xpd_tools.optimization.agent.build_queue_agent
    xpd_tools.optimization.evaluation.XrayUvvisEvaluation

The primary goal is to confirm the queue-server can register
'xray_uvvis_acquire' and that auth/connectivity are correct before a campaign.

Usage::

    export XPD_HTTP_API_KEY=<queue-server key>
    export XPD_PDF_REFERENCES=/path/pdf_references.json   # required Stage 4+
    python test_beamline.py

Optional overrides (defaults from halide_local.json wiring):
    XPD_HTTP_SERVER_URI   default: https://xf28id2-xpd-qs1.nsls2.bnl.gov
    XPD_ZMQ_ADDRESS       default: ipc:///var/lib/bluesky-zmq-proxy/xpd-ipc-in-ipc-out/out.sock
    TILED_PROFILE         default: xpd
    TILED_URI             overrides TILED_PROFILE when set
    TILED_SANDBOX_URI     default: https://tiled.nsls2.bnl.gov
    XPD_PDF_MODE          default: fit  (or: raw)

Stages (first FAIL = broken layer):
    1. Env vars present
    2. Queue-server status() authenticated   <- 401 surfaces HERE
    3. Tiled raw + sandbox clients resolve
    4. XrayUvvisEvaluation constructs        (validates pdf_references / cif)
    5. RemoteDispatcher(zmq) constructs
    6. build_queue_agent() constructs        <- QueueserverAgent
    7. 'xray_uvvis_acquire' registered on worker
"""

from __future__ import annotations

import os
import sys
import traceback

# ---------------------------------------------------------------------------
# Defaults matching halide_local.json / beamline wiring
# ---------------------------------------------------------------------------

_DEFAULT_HTTP_URI = "https://xf28id2-xpd-qs1.nsls2.bnl.gov"
_DEFAULT_ZMQ_ADDR = "ipc:///var/lib/bluesky-zmq-proxy/xpd-ipc-in-ipc-out/out.sock"
_DEFAULT_TILED_PROFILE = "xpd"
_DEFAULT_SANDBOX_URI = "https://tiled.nsls2.bnl.gov"
_SANDBOX_CATALOG = "xpd/sandbox"
_TARGET_PLAN = "xray_uvvis_acquire"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

G, R, Y, X = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

def _ok(m):   print(f"  {G}[PASS]{X} {m}")
def _bad(m):  print(f"  {R}[FAIL]{X} {m}")
def _warn(m): print(f"  {Y}[WARN]{X} {m}")
def _info(m): print(f"  [info] {m}")

def _stage(n, t):
    print(f"\n{'='*64}\nStage {n}: {t}\n{'='*64}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"xpd-tools blop-agent smoke test  |  python {sys.version.split()[0]}  |  pid {os.getpid()}")

    # -----------------------------------------------------------------------
    _stage(1, "Environment variables")
    # -----------------------------------------------------------------------
    http_uri = os.environ.get("XPD_HTTP_SERVER_URI", _DEFAULT_HTTP_URI)
    http_key = os.environ.get("XPD_HTTP_API_KEY", "")
    zmq_addr = os.environ.get("XPD_ZMQ_ADDRESS", _DEFAULT_ZMQ_ADDR)
    raw_prof = os.environ.get("TILED_PROFILE", _DEFAULT_TILED_PROFILE)
    raw_uri  = os.environ.get("TILED_URI", "")
    sbx_uri  = os.environ.get("TILED_SANDBOX_URI", _DEFAULT_SANDBOX_URI)
    pdf_ref  = os.environ.get("XPD_PDF_REFERENCES", "")
    pdf_mode = os.environ.get("XPD_PDF_MODE", "fit")

    _info(f"http_server_uri = {http_uri}")
    _info(f"zmq_address     = {zmq_addr}")
    _info(f"tiled           = {raw_uri or raw_prof}  |  sandbox = {sbx_uri}")
    _info(f"pdf_references  = {pdf_ref or '(unset)'}  |  pdf_mode = {pdf_mode}")

    if not http_key:
        _bad(
            "XPD_HTTP_API_KEY is empty — queue server will reject with 401.\n"
            "       Export the key first:\n"
            "         export XPD_HTTP_API_KEY=<key>"
        )
        return 1
    _ok(f"XPD_HTTP_API_KEY set (len={len(http_key)})")

    # -----------------------------------------------------------------------
    _stage(2, "Queue-server status() authenticated  [401 isolates here]")
    # -----------------------------------------------------------------------
    try:
        from bluesky_queueserver_api.http import REManagerAPI

        api = REManagerAPI(http_server_uri=http_uri)
        api.set_authorization_key(api_key=http_key)
        status = api.status()
    except Exception as exc:
        _bad(f"status() failed: {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg or "permission" in msg:
            _info("-> 401: key missing, invalid, or wrong scope.")
            _info("   Confirm with:")
            _info(
                f'     curl -s -H "Authorization: ApiKey $XPD_HTTP_API_KEY" '
                f"{http_uri}/api/status"
            )
            _info("   If curl also 401s, ask beamline staff for a valid key.")
        elif "ssl" in msg or "certificate" in msg:
            _info("-> TLS error: httpserver cert expired or self-signed.")
            _info(
                "   Try the local HTTP frontend instead:\n"
                "     export XPD_HTTP_SERVER_URI=http://localhost:60610"
            )
        else:
            _info("-> Connection error: is start-re-manager + httpserver running?")
            _info("   Start with:")
            _info("     start-re-manager --startup-module <module> &")
            _info("     uvicorn --host localhost --port 60610 bluesky_httpserver.server:app &")
        return 1

    _ok("authenticated — /status returned")
    _info(f"manager_state         = {status.get('manager_state')}")
    _info(f"worker_environment_up = {status.get('worker_environment_exists')}")
    _info(f"re_state              = {status.get('re_state')}")
    _info(f"items_in_queue        = {status.get('items_in_queue')}")

    env_up = bool(status.get("worker_environment_exists"))
    if not env_up:
        _warn(
            "Worker environment NOT open — plan list in Stage 7 will be empty.\n"
            "       Open it:  api.environment_open()  or  qserver environment open"
        )

    # -----------------------------------------------------------------------
    _stage(3, "Tiled raw + sandbox clients resolve")
    # -----------------------------------------------------------------------
    raw_client = sandbox_client = None
    try:
        from tiled.client import from_profile, from_uri

        raw_client = from_uri(raw_uri) if raw_uri else from_profile(raw_prof)
        _ok(f"raw client resolved: {type(raw_client).__name__}")

        sandbox_root = from_uri(sbx_uri)
        sandbox_client = sandbox_root[_SANDBOX_CATALOG]
        _ok(f"sandbox client resolved: {_SANDBOX_CATALOG}")
    except Exception as exc:
        _bad(f"Tiled resolve failed: {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            _info("-> Tiled 401: profile token missing or expired.")
            _info("   Check ~/.config/tiled/profiles/*.yml")
        elif "profile" in msg:
            _info(f"-> profile '{raw_prof}' not found in ~/.config/tiled/profiles/")
        # Non-fatal for auth check; evaluator needs it. Continue to next stage.
        _info("(Tiled needed for evaluation — fix before running a campaign)")

    # -----------------------------------------------------------------------
    _stage(4, "XrayUvvisEvaluation constructs")
    # -----------------------------------------------------------------------
    if not pdf_ref:
        _warn(
            "XPD_PDF_REFERENCES not set — skipping evaluator + agent build (Stages 4-6).\n"
            "       Set it to run the full check:\n"
            "         export XPD_PDF_REFERENCES=/path/pdf_references.json"
        )
        # Skip to plan registration check — still useful for the auth/worker goal.
        return _stage7_plans(api, env_up)

    try:
        from xpd_tools.optimization.evaluation import XrayUvvisEvaluation

        evaluation = XrayUvvisEvaluation(
            raw_client,
            sandbox_client,
            pdf_ref,
            pdf_mode=pdf_mode,
        )
        _ok(f"evaluator built — phases: {[p.name for p in evaluation.phases]}")
        _info(f"peak_target = {evaluation.peak_target}  |  pdf_mode = {evaluation.pdf_mode}")
    except Exception as exc:
        _bad(f"XrayUvvisEvaluation failed: {exc}")
        _info(
            "pdf_references JSON format:\n"
            '  {"schema_version": 1, "phases": [\n'
            '    {"name": "CsPbBr3", "gr_path": "...", "minimize": false, "cif_path": "..."}\n'
            "  ]}"
        )
        _info("pdf_mode='fit' requires cif_path on every phase.")
        traceback.print_exc()
        return 1

    # -----------------------------------------------------------------------
    _stage(5, "RemoteDispatcher(zmq) constructs")
    # -----------------------------------------------------------------------
    try:
        from bluesky.callbacks.zmq import RemoteDispatcher

        dispatcher = RemoteDispatcher(zmq_addr)
        _ok(f"RemoteDispatcher built: {zmq_addr}")
    except Exception as exc:
        _bad(f"RemoteDispatcher failed: {exc}")
        _info(f"ZMQ address: {zmq_addr}")
        _info("Is the ZMQ proxy running and the socket accessible?")
        traceback.print_exc()
        return 1

    # -----------------------------------------------------------------------
    _stage(6, "build_queue_agent() constructs")
    # -----------------------------------------------------------------------
    try:
        from xpd_tools.optimization.agent import build_queue_agent

        agent = build_queue_agent(evaluation, api, dispatcher)
        _ok("QueueserverAgent constructed")
        _info(f"dofs       = {[d.name for d in agent.dofs]}")
        _info(f"objectives = {[o.name for o in agent.objectives]}")
    except Exception as exc:
        _bad(f"build_queue_agent failed: {exc}")
        traceback.print_exc()
        return 1

    # -----------------------------------------------------------------------
    return _stage7_plans(api, env_up)


def _stage7_plans(api, env_up: bool) -> int:
    _stage(7, f"'{_TARGET_PLAN}' registered on worker")

    try:
        resp = api.plans_allowed(user_group="admin")
        plans = sorted(resp.get("plans_allowed", {}).keys())
    except Exception as exc:
        _bad(f"plans_allowed() failed: {exc}")
        traceback.print_exc()
        return 1

    if plans:
        print("  Registered plans:")
        for p in plans:
            marker = f"  {chr(8592)}" if p == _TARGET_PLAN else ""
            print(f"    {p}{marker}")
    else:
        _warn("Plan list is empty.")
        if not env_up:
            _info("Worker env is closed — open it and re-run to see plans.")
        else:
            _info(
                "Worker env is open but no plans found.\n"
                "       Check the RE manager startup module injects 'xray_uvvis_acquire'.\n"
                "       Also check user_group_permissions.yaml allows the plan."
            )
        return 1

    if _TARGET_PLAN not in plans:
        _bad(f"'{_TARGET_PLAN}' NOT registered on the worker.")
        _info("The RE manager startup module must inject it into the worker namespace.")
        _info("Check user_group_permissions.yaml includes:")
        _info('  allowed_plans:\n    - ":xray_uvvis_acquire"')
        return 1

    _ok(f"'{_TARGET_PLAN}' registered and ready.")
    print(f"\n{'='*64}")
    print(f"\033[32mALL STAGES PASSED — blop-agent queue path ready.\033[0m")
    print(f"{'='*64}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
