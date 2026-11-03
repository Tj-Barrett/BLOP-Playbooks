#!/usr/bin/env python
"""Run ultra1 at 10 uL/min for 120 seconds, then stop.

!! PHYSICAL SAFETY !!
This script commands real pump motion.
Confirm the syringe is loaded and flow path is clear before running.

Usage::

    python test_beamline_pump.py

Device: ultra1  (XF:28IDC-ES:1{Pump:Syrng-Ultra:1}:)
Rate  : 10 uL/min
Time  : 120 s
"""

from __future__ import annotations

import sys
import time

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

G, R, Y, X = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

def _ok(m):   print(f"  {G}[OK]{X}   {m}")
def _bad(m):  print(f"  {R}[ERR]{X}  {m}")
def _warn(m): print(f"  {Y}[WARN]{X} {m}")
def _info(m): print(f"  [info] {m}")

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

RATE_UL_MIN  = 10.0
RATE_UNIT    = "ul/min"
DURATION_SEC = 120.0
ULTRA1_PV    = "XF:28IDC-ES:1{Pump:Syrng-Ultra:1}:"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"\nultra1 pump test  |  {RATE_UL_MIN} {RATE_UNIT} for {DURATION_SEC:.0f} s")
    print("=" * 56)

    # -- Connect ------------------------------------------------------------
    print("\nConnecting to ultra1...")
    try:
        from xpd_tools.optimization.devices.pumps import SyrngUltra
        pump = SyrngUltra(ULTRA1_PV, name="ultra1")
        pump.wait_for_connection(timeout=10.0)
        _ok(f"connected — PV prefix: {ULTRA1_PV}")
    except Exception as exc:
        _bad(f"connection failed: {exc}")
        return 1

    # -- Read current state -------------------------------------------------
    print("\nCurrent pump state:")
    try:
        status = pump.status.get()
        _info(f"status          = {status!r}")
    except Exception as exc:
        _warn(f"could not read status: {exc}")

    try:
        cur_rate = pump.read_infuse_rate.get()
        cur_unit = pump.read_infuse_rate_unit.get()
        _info(f"current rate    = {cur_rate} {cur_unit}")
    except Exception as exc:
        _warn(f"could not read current rate: {exc}")

    # -- Enable comms if disabled -------------------------------------------
    try:
        comm = pump.communication.get()
        _info(f"communication   = {comm!r}")
        if comm == "Disabled":
            _warn("communication disabled — enabling...")
            pump.communication.put(0)
            pump.update_pump.put(6)
            time.sleep(1.0)
            _ok("communication enabled")
    except Exception as exc:
        _warn(f"could not check/set communication: {exc}")

    # -- Set rate -----------------------------------------------------------
    print(f"\nSetting rate to {RATE_UL_MIN} {RATE_UNIT}...")
    try:
        pump.infuse_rate_unit.put(RATE_UNIT, wait=True)
        time.sleep(0.5)
        pump.infuse_rate.put(RATE_UL_MIN, wait=True)
        time.sleep(1.0)

        # Verify readback
        rbv_rate = pump.read_infuse_rate.get()
        rbv_unit = pump.read_infuse_rate_unit.get()
        _ok(f"rate set — readback: {rbv_rate} {rbv_unit}")
    except Exception as exc:
        _bad(f"failed to set rate: {exc}")
        return 1

    # -- Start infusion ----------------------------------------------------
    print("\nStarting infusion...")
    try:
        pump.pump_infuse.put(1, wait=True)
        time.sleep(1.0)
        status_after = pump.status.get()
        _ok(f"infusing — status: {status_after!r}")
    except Exception as exc:
        _bad(f"failed to start infusion: {exc}")
        # Attempt emergency stop before returning
        _warn("attempting emergency stop...")
        try:
            pump.pump_stop.put(1, wait=True)
        except Exception:
            pass
        return 1

    # -- Run for DURATION_SEC with countdown --------------------------------
    print(f"\nRunning for {DURATION_SEC:.0f} s  (Ctrl-C to stop early)\n")
    start = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start
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
        elapsed = time.monotonic() - start
        print(f"\n\n{Y}Interrupted after {elapsed:.0f} s — stopping pump...{X}")
    else:
        print()  # newline after countdown

    # -- Stop pump ----------------------------------------------------------
    print("\nStopping pump...")
    try:
        pump.pump_stop.put(1, wait=True)
        time.sleep(1.0)
        status_final = pump.status.get()
        _ok(f"stopped — status: {status_final!r}")
    except Exception as exc:
        _bad(f"stop command failed: {exc}")
        _warn("!! Manually stop the pump at the controller !!")
        return 1

    print(f"\n{'='*56}")
    print(f"{G}Done.{X}  ultra1 stopped after {min(time.monotonic() - start, DURATION_SEC):.0f} s.")
    print(f"{'='*56}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
