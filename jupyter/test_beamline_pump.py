#!/usr/bin/env python
"""Run ultra1 at 10 µL/min for 120 s via queue-server script_upload.

!! PHYSICAL SAFETY !!
This commands real pump motion.  Confirm the syringe is loaded and the
flow path is clear before running.

How it works
------------
Uploads a self-executing script into the RE worker namespace via
``script_upload``.  The script drives ultra1 directly using the ophyd
device already connected there — no queue, no RunEngine, no plan
registration.

Scope footprint (only write:scripts + read:status required):
    status()          read:status
    script_upload()   write:scripts
    task_result()     read:status

The worker environment must already be open (the script will tell you
if it isn't).

Usage::

    export XPD_HTTP_API_KEY=<queue-server key>
    python test_beamline_pump.py

Optional overrides (defaults from halide wiring):
    XPD_HTTP_SERVER_URI   default: https://xf28id2-xpd-qs1.nsls2.bnl.gov
    PUMP_RATE_UL_MIN      default: 10.0
    PUMP_DURATION_SEC     default: 120.0
"""

from __future__ import annotations

import os
import sys
import time
import textwrap
import traceback

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HTTP_URI     = os.environ.get("XPD_HTTP_SERVER_URI", "https://xf28id2-xpd-qs1.nsls2.bnl.gov")
HTTP_KEY     = os.environ.get("XPD_HTTP_API_KEY", "")
RATE_UL_MIN  = float(os.environ.get("PUMP_RATE_UL_MIN", "10.0"))
DURATION_SEC = float(os.environ.get("PUMP_DURATION_SEC", "120.0"))

TASK_TIMEOUT  = DURATION_SEC + 30.0  # generous: pump run + settle
POLL_INTERVAL = 2.0

# ---------------------------------------------------------------------------
# Script executed inside the RE worker process.
#
# `ultra1` is already in the worker namespace — halide.py exposes it as a
# module-level name from build_halide_devices().
#
# Direct .put() bypasses set_infuse2() rate-table validation, which rejects
# 10 µL/min for the large steel syringe.  The controller itself decides
# whether to accept the rate.
#
# The try/finally guarantees pump_stop fires even if an earlier put raises.
# ---------------------------------------------------------------------------

_WORKER_SCRIPT = textwrap.dedent(f"""\
    import time

    _rate     = {RATE_UL_MIN}
    _duration = {DURATION_SEC}

    try:
        ultra1.infuse_rate_unit.put("ul/min", wait=True)
        ultra1.infuse_rate.put(_rate, wait=True)
        time.sleep(1.0)
        ultra1.pump_infuse.put(1, wait=True)
        time.sleep(_duration)
    finally:
        ultra1.pump_stop.put(1, wait=True)
""")

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

G, R, Y, X = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

def _ok(m):   print(f"  {G}[OK]{X}   {m}")
def _bad(m):  print(f"  {R}[ERR]{X}  {m}")
def _warn(m): print(f"  {Y}[WARN]{X} {m}")
def _info(m): print(f"  [info] {m}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(
        f"\nultra1 pump test via script_upload\n"
        f"  rate     = {RATE_UL_MIN} µL/min\n"
        f"  duration = {DURATION_SEC:.0f} s\n"
        f"  server   = {HTTP_URI}\n"
        + "=" * 56
    )

    if not HTTP_KEY:
        _bad(
            "XPD_HTTP_API_KEY is empty — queue server will reject with 401.\n"
            "       export XPD_HTTP_API_KEY=<key>"
        )
        return 1

    try:
        from bluesky_queueserver_api.http import REManagerAPI
    except ImportError as exc:
        _bad(f"bluesky_queueserver_api not importable: {exc}")
        return 1

    api = REManagerAPI(http_server_uri=HTTP_URI)
    api.set_authorization_key(api_key=HTTP_KEY)

    # -- Auth / status check ------------------------------------------------
    print("\nChecking queue-server status...")
    try:
        status = api.status()
    except Exception as exc:
        _bad(f"status() failed: {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            _info("-> 401: key missing or invalid.")
            _info(f'   curl -s -H "Authorization: ApiKey $XPD_HTTP_API_KEY" {HTTP_URI}/api/status')
        return 1

    _ok("connected")
    _info(f"manager_state = {status.get('manager_state')}")
    _info(f"env_exists    = {status.get('worker_environment_exists')}")
    _info(f"re_state      = {status.get('re_state')}")

    if not status.get("worker_environment_exists"):
        _bad(
            "Worker environment is not open.\n"
            "       Someone with write:manager:control must run:\n"
            "         qserver environment open\n"
            "       then re-run this script."
        )
        return 1

    manager_state = status.get("manager_state", "")
    if manager_state != "idle":
        _bad(
            f"Manager is not idle (state={manager_state!r}).\n"
            "       script_upload is refused while a plan is running.\n"
            "       Wait for the current run to finish, then retry."
        )
        return 1

    _ok("worker env open and idle — ready to upload")

    # -- Upload and execute the pump script ---------------------------------
    print(f"\nUploading pump script ({DURATION_SEC:.0f} s run)...")
    print("  Script to execute in worker:")
    for line in _WORKER_SCRIPT.splitlines():
        print(f"    {line}")
    print()

    try:
        resp = api.script_upload(_WORKER_SCRIPT, update_lists=False)
        task_uid = resp.get("task_uid")
        if not task_uid:
            _bad(f"script_upload returned no task_uid: {resp}")
            return 1
        _ok(f"uploaded — task_uid: {task_uid}")
    except Exception as exc:
        _bad(f"script_upload() failed: {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            _info("-> 401: key lacks write:scripts scope.")
        traceback.print_exc()
        return 1

    # -- Poll task_result until done ----------------------------------------
    print(
        f"\nPump running in worker process for {DURATION_SEC:.0f} s.\n"
        "Polling task status every 2 s  (script will stop pump on completion).\n"
    )
    start = time.monotonic()
    result = None
    try:
        while True:
            elapsed = time.monotonic() - start
            remaining = DURATION_SEC - elapsed

            print(
                f"\r  t={elapsed:5.0f}s  remaining≈{max(remaining, 0):5.0f}s",
                end="",
                flush=True,
            )

            try:
                resp = api.task_result(task_uid=task_uid)
                task_status = resp.get("status", "")
                if task_status == "completed":
                    result = resp.get("result", {})
                    break
            except Exception as exc:
                _warn(f"\npoll failed: {exc}")

            if elapsed > TASK_TIMEOUT:
                _warn(f"\ntask_result timed out after {elapsed:.0f} s")
                break

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n\n{Y}Interrupted.{X}")
        _warn(
            "The script is running inside the worker — it cannot be cancelled\n"
            "       remotely without write:manager:control scope.\n"
            "       The pump will stop automatically when the script finishes\n"
            f"       (at most {TASK_TIMEOUT:.0f} s from upload).\n"
            "       To stop immediately: use the pump controller directly."
        )
        return 1

    print()
    elapsed = time.monotonic() - start

    # -- Interpret result ---------------------------------------------------
    if result is None:
        _warn("No result received — pump may still be running or task timed out")
        return 1

    success = result.get("success", False)
    msg     = result.get("msg", "")

    if success:
        _ok(f"Script completed successfully in {elapsed:.0f} s")
    else:
        _bad(f"Script failed: {msg}")
        tb = result.get("traceback", "")
        if tb:
            print(tb)
        return 1

    print(f"\n{'='*56}")
    print(f"{G}Done.{X}  ultra1 stopped after {elapsed:.0f} s.")
    print(f"{'='*56}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
