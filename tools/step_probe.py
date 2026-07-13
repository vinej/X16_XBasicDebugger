#!/usr/bin/env python3
"""M2: proof of source-level stepping for XBasic over the binary monitor.

Using the M1 source map (xcbmap.py), this probe:
  1. launches the Box16 fork with the PRG on its command line,
  2. resets the machine to a paused state (re-arming the -prg boot injection)
     so the checkpoint is armed BEFORE the program runs -- essential for
     run-once programs like demo.bas (the Prog8 probe armed after -run, which
     only works for forever-looping demos),
  3. arms an exec checkpoint on a mapped .bas line's address and resumes,
  4. maps the stop PC back to the .bas line (must round-trip),
  5. steps (step-over, so jsr = one step) until the mapped line changes.

    python tools/step_probe.py --line 14            # against examples/demo
    python tools/step_probe.py --map examples/demo.xcbmap.json --line 14
"""

import argparse
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binmon import Monitor
from xcbmap import SourceMap

REPO = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
DEF_MAP = os.path.join(REPO, "examples", "demo.xcbmap.json")
DEF_PRG = os.path.join(REPO, "examples", "demo.prg")
DEF_BOX16 = os.path.join(REPO, "emulator", "box16.exe")
DEF_ROM = os.path.join(REPO, "emulator", "rom.bin")


def fmt(smap, pc):
    e = smap.addr_to_entry(pc)
    if e is None:
        return f"${pc:04x} -> (unmapped)"
    return f"${pc:04x} -> {os.path.basename(e['file'])}:{e['line']}"


def wait_for_port(host, port, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection((host, port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"monitor port {host}:{port} never opened")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default=DEF_MAP, help="xcbmap.json from xcbmap.py")
    ap.add_argument("--file", default="demo.bas", help=".bas file (suffix match)")
    ap.add_argument("--line", type=int, default=14, help=".bas line to break on")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6502)
    ap.add_argument("--box16", default=DEF_BOX16)
    ap.add_argument("--rom", default=DEF_ROM)
    ap.add_argument("--prg", default=DEF_PRG)
    ap.add_argument("--attach", action="store_true",
                    help="attach to a running Box16 instead of launching")
    ap.add_argument("--keep", action="store_true",
                    help="leave Box16 running afterwards")
    ap.add_argument("--max-steps", type=int, default=200)
    args = ap.parse_args()

    smap = SourceMap.load(args.map)
    entry = smap.line_to_entry(args.file, args.line)
    if entry is None:
        entry = smap.next_mapped_line(args.file, args.line)
        if entry is None:
            sys.exit(f"{args.file}:{args.line} has no mapped statement")
        print(f"{args.file}:{args.line} is not a statement; adjusted to "
              f"line {entry['line']} (as a DAP adapter would)")
    addr = entry["addr"]
    print(f"target {os.path.basename(entry['file'])}:{entry['line']} "
          f"-> ${addr:04x}")

    box16 = None
    if not args.attach:
        cmd = [args.box16, "-ignore_ini", "-binarymonitor",
               "-rom", args.rom, "-prg", args.prg, "-run", "-scale", "1"]
        print(f"launching {os.path.basename(args.box16)} ...")
        box16 = subprocess.Popen(cmd, cwd=os.path.dirname(args.box16),
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        wait_for_port(args.host, args.port)

    mon = Monitor(args.host, args.port)
    ok = False
    try:
        mon.ping()
        # Reset to paused (re-arms -prg boot injection) so we can arm the
        # checkpoint before the program starts -- works for run-once programs.
        mon.reset_paused()
        cp = mon.checkpoint_set(addr)      # exec, low RAM, no bank needed
        mon.resume()
        print(f"checkpoint {cp} set at ${addr:04x}, program running ...")
        pc = mon.wait_stopped(timeout=60)
        print(f"HIT  {fmt(smap, pc)}")
        hit = smap.addr_to_entry(pc)
        if pc != addr or hit is None or hit["addr"] != addr:
            raise AssertionError("stop PC did not round-trip to the target line")
        if hit["line"] != entry["line"]:
            print(f"  (address shared with {hit['file']}:{hit['line']}; "
                  "stepping until the line leaves that one)")
            entry = hit

        print("stepping (step-over) until the line changes:")
        steps = 0
        while steps < args.max_steps:
            pc = mon.advance(step_over=True)
            steps += 1
            cur = smap.addr_to_entry(pc)
            print(f"  step {steps}: {fmt(smap, pc)}")
            if cur is not None and (cur["line"] != entry["line"]
                                    or cur["file"] != entry["file"]):
                print(f"LINE CHANGED after {steps} step(s): "
                      f"{entry['line']} -> {cur['line']}")
                ok = True
                break
        else:
            raise AssertionError(f"line never changed in {args.max_steps} steps")

        mon.checkpoint_delete(cp)
        mon.resume()
        print("checkpoint removed, program resumed")
    finally:
        mon.close()
        if box16 is not None and not args.keep:
            box16.terminate()
            try:
                box16.wait(timeout=5)
            except subprocess.TimeoutExpired:
                box16.kill()
            print("box16 terminated")

    print("M2 PASS" if ok else "M2 FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
