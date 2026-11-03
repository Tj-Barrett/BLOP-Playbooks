#!/usr/bin/env python
"""Submit pump_run via queue server and tail status until it finishes.

Runs ultra1 at 10 µL/min for 120 s through the RE manager so you can go
physically check flow.

!! PHYSICAL SAFETY !!
This commands real pump motion.  Confirm the syringe is loaded and the
flow path is clear before running.

How it works
------------
1. Uploads a tiny ``pump_run`` plan definition into the worker namespace
   via ``api.script_upload()`` — no restart, no changes to any startup
   module.
2. Submits ``BPlan("pump_run", ...)`` to the queue.
3. Polls until done, Ctrl-C safely stops and aborts.

Usage::

    export XPD_HTTP_API_KEY=<queue-server key>
    python test_beamline_pump.py

Optional overrides (defaults from halide wiring):
    XPD_HTTP_SERVER_URI   default: https://xf28id2-xpd-qs1.nsls2.bnl.gov
    PUMP_NAME             default: ultra1
    PUMP_RATE_UL_MIN      default: 10.0
    PUMP_DURATION_SEC     default: 120.0
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import textwrap

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HTTP_URI     = os.environ.get("XPD_HTTP_SERVER_URI", "https://xf28id2-xpd-qs1.nsls2.bnl.gov")
HTTP_KEY     = os.environ.get("XPD_HTTP_API_KEY", "")
PUMP_NAME    = os.environ.get("PUMP_NAME", "ultra1")
RATE_UL_MIN  = float(os.environ.get("PUMP_RATE_UL_MIN", "10.0"))
DURATION_SEC = float(os.environ.get("PUMP_DURATION_SEC", "120.0"))

POLL_INTERVAL = 2.0  # seconds between status polls

# ---------------------------------------------------------------------------
# The plan uploaded into the worker namespace via script_upload.
#
# It runs in the worker process, so it has access to whatever the startup
# module already put in globals() — specifically the `devices` dict from
# build_halide_devices(), which contains ultra1, ultra2, etc.
#
# Bypasses set_infuse2() rate-table validation intentionally: 10 µL/min is
# below the minimum for a large steel syringe, so we write IRATE/IRATEUNITS
# directly and let the controller accept or reject the rate.
# ---------------------------------------------------------------------------

_PUMP_RUN_SCRIPT = textwrap.dedent("""\
    import bluesky.plan_stubs as bps

    def pump_run(pump_name="ultra1", rate_ul_min=10.0, duration_sec=120.0):
        \"\"\"Infuse a named Ultra pump at a fixed rate for a fixed duration.\"\"\"
        pump = devices.get(pump_name)
        if pump is None:
            raise ValueError(
                f"pump_run: unknown pump {pump_name!r}. "
                f"Available: {list(devices)}"
            )
        yield from bps.abs_set(pump.infuse_rate_unit, "ul/min", wait=True)
        yield from bps.abs_set(pump.infuse_rate, float(rate_ul_min), wait=True)
        yield from bps.sleep(1.0)
        yield from bps.abs_set(pump.pump_infuse, 1, wait=True)
        yield from bps.sleep(float(duration_sec))
        yield from bps.abs_set(pump.pump_stop, 1, wait=True)
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
        f"\npump_run via queue server\n"
        f"  pump     = {PUMP_NAME}\n"
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
        from bluesky_queueserver_api import BPlan
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
    _info(f"queue_items   = {status.get('items_in_queue')}")

    if not status.get("worker_environment_exists"):
        _warn("Worker environment not open — opening now...")
        try:
            api.environment_open()
            _wait_for_env_open(api, timeout=30)
        except Exception as exc:
            _bad(f"environment_open() failed: {exc}")
            return 1

    # -- Upload pump_run into the worker namespace --------------------------
    print("\nUploading pump_run into worker namespace...")
    try:
        resp = api.script_upload(_PUMP_RUN_SCRIPT, update_lists=True)
        task_uid = resp.get("task_uid")
        _ok(f"script_upload accepted — task_uid: {task_uid}")
    except Exception as exc:
        _bad(f"script_upload() failed: {exc}")
        traceback.print_exc()
        return 1

    # Wait for the upload task to complete
    if not _wait_for_task(api, task_uid, timeout=15):
        _bad("script_upload task did not complete — pump_run may not be registered")
        return 1
    _ok("pump_run registered in worker namespace")

    # -- Confirm pump_run is now in allowed plans ---------------------------
    print("\nVerifying pump_run is allowed...")
    try:
        resp = api.plans_allowed(user_group="admin")
        plans = resp.get("plans_allowed", {})
        if "pump_run" not in plans:
            _bad(
                "'pump_run' not in allowed plans after upload.\n"
                "       Add ':pump_run' to user_group_permissions.yaml, or use a\n"
                "       wildcard like ':.*' that covers it."
            )
            _info(f"Current allowed plans: {sorted(plans)}")
            return 1
        _ok("pump_run allowed")
    except Exception as exc:
        _bad(f"plans_allowed() failed: {exc}")
        return 1

    # -- Clear stale queue items and add pump_run --------------------------
    print("\nAdding pump_run to queue...")
    try:
        queue = api.queue_get()
        if queue.get("items"):
            _warn(f"{len(queue['items'])} stale item(s) in queue — clearing")
            api.queue_clear()

        item = BPlan(
            "pump_run",
            pump_name=PUMP_NAME,
            rate_ul_min=RATE_UL_MIN,
            duration_sec=DURATION_SEC,
        )
        resp = api.item_add(item)
        item_uid = resp.get("item", {}).get("item_uid")
        _ok(f"queued — item_uid: {item_uid}")
    except Exception as exc:
        _bad(f"item_add() failed: {exc}")
        traceback.print_exc()
        return 1

    # -- Start queue --------------------------------------------------------
    print("\nStarting queue...")
    try:
        api.queue_start()
        _ok("queue started")
    except Exception as exc:
        _bad(f"queue_start() failed: {exc}")
        traceback.print_exc()
        return 1

    # -- Poll until done ----------------------------------------------------
    print(
        f"\nRunning pump_run ({PUMP_NAME}, {RATE_UL_MIN} µL/min, {DURATION_SEC:.0f} s)\n"
        "Polling every 2 s. Ctrl-C to stop the pump and abort.\n"
    )
    start = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start
            try:
                status = api.status()
            except Exception as exc:
                _warn(f"poll failed: {exc}")
                time.sleep(POLL_INTERVAL)
                continue

            manager_state = status.get("manager_state", "")
            re_state      = status.get("re_state", "")
            queue_items   = status.get("items_in_queue", "?")

            print(
                f"\r  t={elapsed:5.0f}s  manager={manager_state:<20}  "
                f"RE={re_state:<12}  queue={queue_items}",
                end="",
                flush=True,
            )

            if manager_state == "idle" and int(queue_items or 0) == 0:
                break

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n\n{Y}Interrupted — stopping queue and aborting run...{X}")
        try:
            api.re_stop()
            time.sleep(1)
            api.queue_stop()
        except Exception as exc:
            _warn(f"stop/abort failed: {exc}")
            _warn("!! Manually stop pump at the controller !!")
        return 1

    print()
    elapsed = time.monotonic() - start

    # -- Check history for result ------------------------------------------
    try:
        history = api.history_get()
        items = history.get("items", [])
        if items:
            last = items[-1]
            result = last.get("result", {})
            exit_status = result.get("exit_status", "unknown")
            msg = result.get("msg", "")
            if exit_status == "completed":
                _ok(f"pump_run completed in {elapsed:.0f} s")
            else:
                _bad(f"pump_run ended: status={exit_status!r}  msg={msg!r}")
                return 1
        else:
            _warn("history empty — could not confirm result")
    except Exception as exc:
        _warn(f"history_get() failed: {exc}")

    print(f"\n{'='*56}")
    print(f"{G}Done.{X}  {PUMP_NAME} stopped after {elapsed:.0f} s.")
    print(f"{'='*56}\n")
    return 0


def _wait_for_env_open(api, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = api.status()
        if s.get("worker_environment_exists"):
            _ok("worker environment open")
            return
        time.sleep(1.0)
    raise TimeoutError("worker environment did not open within timeout")


def _wait_for_task(api, task_uid: str, timeout: float = 15.0) -> bool:
    """Poll task_result until the task is done. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = api.task_result(task_uid=task_uid)
            if resp.get("status") == "completed":
                result = resp.get("result", {})
                if result.get("success"):
                    return True
                else:
                    _bad(f"script_upload task failed: {result.get('msg', '')}")
                    return False
        except Exception:
            pass
        time.sleep(1.0)
    _warn("task_result timed out")
    return False


if __name__ == "__main__":
    sys.exit(main())
