#!/usr/bin/env python3
"""End-to-end smoke test for tools/dap_adapter.py (XBasic).

Plays VSCode: spawns the adapter on stdio, compiles + launches demo.bas in
the real Box16 fork, and asserts a full debug session:

  stopOnEntry -> breakpoints (14, 16) -> variables (Globals) -> setVariable
  -> next (line advances) -> continue (breakpoint) -> disconnect (Box16 gone).

Run:  python test/dap_smoke.py
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ADAPTER = os.path.join(REPO, "tools", "dap_adapter.py")
PROGRAM = os.path.join(REPO, "examples", "demo.bas")


class DapClient:
    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, ADAPTER],
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.seq = 0
        self.incoming = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        out = self.proc.stdout
        while True:
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                ch = out.read(1)
                if not ch:
                    return
                headers += ch
            length = int(headers.decode().split(":")[1].strip().split("\r\n")[0])
            self.incoming.put(json.loads(out.read(length)))

    def request(self, command, arguments=None):
        self.seq += 1
        msg = {"seq": self.seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        data = json.dumps(msg).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.proc.stdin.flush()
        return self.seq

    def wait(self, pred, what, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("waiting for " + what)
            try:
                msg = self.incoming.get(timeout=remaining)
            except queue.Empty:
                continue
            if pred(msg):
                return msg

    def wait_response(self, req_seq, timeout=90):
        m = self.wait(lambda m: m.get("type") == "response"
                      and m.get("request_seq") == req_seq,
                      f"response {req_seq}", timeout)
        assert m["success"], f"request failed: {m.get('message')} / {m}"
        return m

    def wait_event(self, event, timeout=90):
        return self.wait(lambda m: m.get("type") == "event"
                         and m.get("event") == event, f"event {event}", timeout)


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


def stack_line(c):
    rs = c.request("stackTrace", {"threadId": 1})
    frames = c.wait_response(rs)["body"]["stackFrames"]
    assert frames, "empty stack"
    f = frames[0]
    src = f.get("source", {}).get("path", "(none)")
    print(f"     top frame: {f['name']} at {os.path.basename(src)}:{f['line']}")
    return f


def main():
    c = DapClient()
    r = c.request("initialize", {"adapterID": "xcbasic"})
    body = c.wait_response(r).get("body", {})
    check(body.get("supportsConfigurationDoneRequest"), "initialize capabilities")

    launch_seq = c.request("launch", {"program": PROGRAM, "stopOnEntry": True})
    c.wait_event("initialized", timeout=120)
    check(True, "launch -> initialized (compiled, Box16 up, reset done)")

    r = c.request("setBreakpoints", {
        "source": {"path": PROGRAM},
        "breakpoints": [{"line": 14}, {"line": 16}]})
    bps = c.wait_response(r)["body"]["breakpoints"]
    check(bps[0]["verified"] and bps[0]["line"] == 14, "breakpoint line 14 verified")
    check(bps[1]["verified"] and bps[1]["line"] == 16, "breakpoint line 16 verified")

    r = c.request("configurationDone")
    c.wait_response(r)
    c.wait_response(launch_seq)
    check(True, "configurationDone + launch responses")

    ev = c.wait_event("stopped", timeout=90)
    check(ev["body"]["reason"] == "entry", "stopOnEntry stop")
    stack_line(c)

    r = c.request("continue", {"threadId": 1})
    c.wait_response(r)
    ev = c.wait_event("stopped", timeout=90)
    check(ev["body"]["reason"] == "breakpoint", "breakpoint stop reason")
    f = stack_line(c)
    check(f["line"] == 14, "stopped on line 14")

    # --- variables (M4) --------------------------------------------------
    r = c.request("scopes", {"frameId": 1})
    scopes = c.wait_response(r)["body"]["scopes"]
    gscope = next((s for s in scopes if s["name"] == "Globals"), None)
    check(gscope is not None, f"Globals scope present: {[s['name'] for s in scopes]}")

    r = c.request("variables", {"variablesReference": gscope["variablesReference"]})
    gvars = {v["name"]: v for v in c.wait_response(r)["body"]["variables"]}
    print("     globals: " + ", ".join(f"{n}={v['value']}" for n, v in gvars.items()))
    check({"total", "count", "msg", "i"} <= set(gvars), "total/count/msg/i present")
    ival = int(gvars["i"]["value"].split()[0])
    check(1 <= ival <= 10, f"loop var i plausible ({ival})")
    check("sum" in gvars["msg"]["value"].lower(),
          f"string var msg = {gvars['msg']['value']} (PETSCII)")
    check(gvars["total"]["type"] == "long" and gvars["count"]["type"] == "byte",
          "typed formatting (long / byte)")

    r = c.request("setVariable", {
        "variablesReference": gscope["variablesReference"],
        "name": "count", "value": "7"})
    setr = c.wait_response(r)["body"]
    check(setr["value"].split()[0] == "7", f"setVariable count -> {setr['value']}")

    r = c.request("evaluate", {"expression": "total", "context": "hover",
                               "frameId": 1})
    ev = c.wait_response(r)["body"]
    check(ev["result"] != "", f"hover evaluate total = {ev['result']}")

    r = c.request("next", {"threadId": 1})
    c.wait_response(r)
    c.wait_event("stopped", timeout=90)
    f = stack_line(c)
    check(f["line"] > 14, f"next: line advanced 14 -> {f['line']}")

    r = c.request("continue", {"threadId": 1})
    c.wait_response(r)
    ev = c.wait_event("stopped", timeout=90)
    check(ev["body"]["reason"] == "breakpoint", "continue -> breakpoint")
    stack_line(c)

    r = c.request("disconnect", {"terminateDebuggee": True})
    c.wait_response(r)
    c.proc.wait(timeout=15)
    check(c.proc.returncode is not None, "adapter exited on disconnect")

    time.sleep(1)
    tasks = subprocess.run(["tasklist", "/FI", "IMAGENAME eq box16.exe"],
                           capture_output=True, text=True).stdout
    check("box16.exe" not in tasks, "Box16 terminated")
    print("DONE - DAP smoke test passed")


if __name__ == "__main__":
    main()
