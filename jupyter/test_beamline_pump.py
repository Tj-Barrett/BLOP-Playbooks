#!/usr/bin/env python
"""Run ultra1 at 10 µL/min for 120 s via queue-server script_upload.

!! PHYSICAL SAFETY !!
This commands real pump motion.  Confirm the syringe is loaded and the
flow path is clear before running.

How it works
------------
Uploads a self-executing script into the RE worker namespace via a single
``script_upload`` call.  No status checks, no polling — only write:scripts
scope is required.

The uploaded script drives ultra1 directly using the ophyd device already
connected in the worker (halide.py exposes ``ultra1`` as a module global).
A try/finally guarantees pump_stop fires even if an earlier put raises.

After uploading the client runs a local countdown so you know when to
expect the pump to stop.  Verify flow physically / at the controller.

Requirements
------------
- Worker environment must already be open.
- Worker must be idle (no plan running).
- API key needs: write:scripts  (only)

Usage::

    export XPD_HTTP_API_KEY=<queue-server key>
    python test_beamline_pump.py

Optional overrides:
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

# ---------------------------------------------------------------------------
# Script executed inside the RE worker process.
# ultra1 is already in the worker namespace (halide.py module global).
# Direct .put() bypasses set_infuse2() rate-table validation.
# ---------------------------------------------------------------------------

_WORKER_SCRIPT = textwrap.dedent(f"""\
    import time

    try:
        ultra1.infuse_rate_unit.put("ul/min", wait=True)
        ultra1.infuse_rate.put({RATE_UL_MIN}, wait=True)
        time.sleep(1.0)
        ultra1.pump_infuse.put(1, wait=True)
        time.sleep({DURATION_SEC})
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
            "XPD_HTTP_API_KEY is empty.\n"
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

    # -- Upload (the only API call) ----------------------------------------
    print("\nScript to execute in worker:")
    for line in _WORKER_SCRIPT.splitlines():
        print(f"    {line}")
    print()
    print("Uploading...")

    try:
        resp = api.script_upload(_WORKER_SCRIPT, update_lists=False)
    except Exception as exc:
        _bad(f"script_upload() failed: {exc}")
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            _info("-> 401: key lacks write:scripts scope, or key is invalid.")
        elif "409" in msg or "conflict" in msg:
            _info("-> 409: worker is busy (plan running) or env is closed.")
            _info("   Wait for the current run to finish and retry.")
        traceback.print_exc()
        return 1

    task_uid = resp.get("task_uid", "(none)")
    _ok(f"uploaded — task_uid: {task_uid}")
    _info("Pump is now running in the worker process.")
    _info("Verify flow physically / at the controller.")

    # -- Local countdown (informational only) ------------------------------
    print(f"\nLocal countdown for {DURATION_SEC:.0f} s  (Ctrl-C to exit this script)\n")
    start = time.monotonic()
    try:
        while True:
            elapsed   = time.monotonic() - start
            remaining = DURATION_SEC - elapsed
            if remaining <= 0:
                break
            print(
                f"\r  elapsed: {elapsed:5.0f} s  |  remaining: {remaining:5.0f} s",
                end="",
                flush=True,
            )
            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"\n\n{Y}Local countdown interrupted.{X}")
        _warn(
            "The script is still running inside the worker.\n"
            "       The pump will stop automatically when the worker script finishes\n"
            f"       (within {DURATION_SEC:.0f} s of upload).\n"
            "       To stop immediately: use the pump controller directly."
        )
        return 0

    print()
    _ok(f"Countdown complete — pump should have stopped ({DURATION_SEC:.0f} s elapsed).")
    _info("Check the controller to confirm.")

    print(f"\n{'='*56}")
    print(f"{G}Done.{X}")
    print(f"{'='*56}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
