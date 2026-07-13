#!/usr/bin/env python3
"""M3/M4: DAP debug adapter for XC=BASIC ("XBasic") on the Commander X16.

Speaks the Debug Adapter Protocol on stdio (launched by the VSCode
extension in this repo) and the VICE binary monitor to the Box16 fork.
Reuses the proven pieces: xcbmap.py (M1 source map, which also compiles the
.bas on launch) and the monitor framing from binmon.py (M2).

Launch config (see package.json for the schema):
    program     .bas source file -- compiled + mapped by xcbmap on launch
    target      XC=BASIC target (default "x16")
    box16/rom   default: this repo's emulator/ fork build + rom
    xcbasic/dasm overrides for the debug-info compiler + assembler
    stopOnEntry break on the program's first mapped statement
    port/host   monitor endpoint (default 127.0.0.1:6502)
    scale       Box16 window scale (default 1)

Design mirrors the Prog8 adapter: one reader thread per input (DAP stdin,
monitor socket) feeds a single work queue; the main loop is the only thing
that talks back to VSCode or issues monitor commands, so no locking is
needed. Step-over/in/out loop at instruction level until the mapped line
changes.
"""

import json
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binmon
from binmon import (CMD_ADVANCE, CMD_CHECKPOINT_DELETE, CMD_CHECKPOINT_SET,
                    CMD_EXIT, CMD_MEMORY_GET, CMD_MEMORY_SET, CMD_PING,
                    CMD_RESET, CMD_UNTIL_RETURN, EVENT_ID,
                    RESP_RESUMED, RESP_STOPPED)
import xcbmap
from xcbmap import TYPE_WIDTH

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEF_BOX16 = os.path.join(REPO, "emulator", "box16.exe")
DEF_ROM = os.path.join(REPO, "emulator", "rom.bin")

THREAD_ID = 1
STEP_CAP = 5000
LOG = os.environ.get("XCBASIC_DAP_LOG")

_PROC_RE = re.compile(r"^\s*(sub|function)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_ENDPROC_RE = re.compile(r"^\s*end\s+(sub|function)\b", re.I)


def log(msg):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


class ThreadedMonitor:
    """binmon framing with a dedicated reader thread: responses are routed
    to waiting callers by request id, events go to the shared work queue
    as ('mon', rtype, err, body)."""

    def __init__(self, host, port, work_queue):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.2)
        self.work = work_queue
        self.next_id = 1
        self.pending = {}
        self.closed = False
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_loop(self):
        buf = b""
        while not self.closed:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                continue
            except OSError:
                break
            while len(buf) >= 12:
                stx, api, body_len = struct.unpack_from("<BBI", buf, 0)
                if stx != binmon.STX or api != binmon.API:
                    log(f"monitor: bad frame header {buf[:2].hex()}")
                    return
                total = 12 + body_len
                if len(buf) < total:
                    break
                rtype, err, rid = struct.unpack_from("<BBI", buf, 6)
                body = buf[12:total]
                buf = buf[total:]
                if rid == EVENT_ID:
                    self.work.put(("mon", rtype, err, body))
                elif rid in self.pending:
                    self.pending.pop(rid).put((rtype, err, body))

    def command(self, cmd, body=b"", timeout=5.0):
        rq = queue.Queue(maxsize=1)
        req_id = self.next_id
        self.next_id += 1
        self.pending[req_id] = rq
        frame = struct.pack("<BBIIB", binmon.STX, binmon.API,
                            len(body), req_id, cmd) + body
        self.sock.sendall(frame)
        try:
            rtype, err, rbody = rq.get(timeout=timeout)
        except queue.Empty:
            self.pending.pop(req_id, None)
            raise TimeoutError(f"monitor command {cmd:#04x} timed out")
        if err != 0:
            raise binmon.MonitorError(f"command {cmd:#04x} -> error {err:#04x}")
        return rbody

    def ping(self):
        self.command(CMD_PING)

    def memory_get(self, start, end):
        body = struct.pack("<BHHBH", 0, start, end, 0, 0)
        rbody = self.command(CMD_MEMORY_GET, body)
        (n,) = struct.unpack_from("<H", rbody, 0)
        return rbody[2:2 + n]

    def memory_set(self, start, data):
        body = struct.pack("<BHHBH", 0, start, start + len(data) - 1, 0, 0)
        self.command(CMD_MEMORY_SET, body + bytes(data))

    def checkpoint_set(self, start, end=None, op=4, temporary=False, bank=None):
        end = start if end is None else end
        body = struct.pack("<HHBBBBB", start, end, 1, 1, op, int(temporary), 0)
        if bank is not None:
            body += struct.pack("<H", bank)
        rbody = self.command(CMD_CHECKPOINT_SET, body)
        (cp_num,) = struct.unpack_from("<I", rbody, 0)
        return cp_num

    def checkpoint_delete(self, cp_num):
        self.command(CMD_CHECKPOINT_DELETE, struct.pack("<I", cp_num))

    def resume(self):
        self.command(CMD_EXIT)

    def advance(self, step_over=False, count=1):
        self.command(CMD_ADVANCE, struct.pack("<BH", int(step_over), count))

    def until_return(self):
        self.command(CMD_UNTIL_RETURN)

    def reset_paused(self):
        self.command(CMD_RESET, b"\x00")


class Adapter:
    LOCALS_REF, GLOBALS_REF = 1001, 1002

    def __init__(self):
        self.work = queue.Queue()
        self.seq = 0
        self.out_lock = threading.Lock()
        self.mon = None
        self.box16 = None
        self.smap = None
        self.cfg = {}
        self.cwd = None
        self.prg = None
        self.breakpoints = {}       # source path -> {line: cp_num}
        self.entry_cp = None
        self.stopped_pc = None
        self.launch_req = None
        self.running = True
        self._src_cache = {}
        self._vars_by_ref = {}

    # -- DAP wire --------------------------------------------------------

    def _send(self, msg):
        data = json.dumps(msg).encode("utf-8")
        with self.out_lock:
            sys.stdout.buffer.write(
                b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
            sys.stdout.buffer.flush()
        log(f"-> {msg.get('type')} {msg.get('command') or msg.get('event')}")

    def send_response(self, request, body=None, success=True, message=None):
        self.seq += 1
        msg = {"seq": self.seq, "type": "response",
               "request_seq": request["seq"], "command": request["command"],
               "success": success}
        if body is not None:
            msg["body"] = body
        if message:
            msg["message"] = message
        self._send(msg)

    def send_event(self, event, body=None):
        self.seq += 1
        msg = {"seq": self.seq, "type": "event", "event": event}
        if body is not None:
            msg["body"] = body
        self._send(msg)

    def output(self, text, category="console"):
        self.send_event("output", {"category": category, "output": text + "\n"})

    # -- stdin reader ------------------------------------------------------

    def stdin_loop(self):
        stream = sys.stdin.buffer
        while True:
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                ch = stream.read(1)
                if not ch:
                    self.work.put(("dap-eof",))
                    return
                headers += ch
            length = 0
            for line in headers.decode().split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":")[1])
            data = stream.read(length)
            try:
                self.work.put(("dap", json.loads(data)))
            except ValueError:
                log(f"bad DAP payload: {data[:200]!r}")

    # -- monitor event helpers --------------------------------------------

    def wait_mon(self, rtype, timeout=30.0):
        deferred = []
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"monitor event {rtype:#04x} timed out")
                try:
                    item = self.work.get(timeout=remaining)
                except queue.Empty:
                    continue
                if item[0] == "mon":
                    _, etype, err, body = item
                    if etype == rtype:
                        return err, body
                else:
                    deferred.append(item)
        finally:
            for item in deferred:
                self.work.put(item)

    def wait_stopped(self, timeout=30.0):
        err, body = self.wait_mon(RESP_STOPPED, timeout)
        (pc,) = struct.unpack_from("<H", body, 0)
        return pc

    # -- source helpers ----------------------------------------------------

    def _resolve(self, file):
        return file if os.path.isabs(file) else os.path.normpath(
            os.path.join(self.cwd, file))

    def _src_lines(self, file):
        path = self._resolve(file)
        if path not in self._src_cache:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    self._src_cache[path] = f.read().splitlines()
            except OSError:
                self._src_cache[path] = []
        return self._src_cache[path]

    def enclosing_proc(self, entry):
        """Name of the SUB/FUNCTION containing this line, or None (program
        scope). XBasic subs do not nest."""
        if entry is None:
            return None
        lines = self._src_lines(entry["file"])
        current = None
        for i in range(min(entry["line"], len(lines))):
            m = _PROC_RE.match(lines[i])
            if m:
                current = m.group(2)
            elif _ENDPROC_RE.match(lines[i]):
                current = None
        return current

    def entry_source(self, entry):
        path = self._resolve(entry["file"])
        return {"name": os.path.basename(path), "path": path}

    def frame_name(self, entry):
        proc = self.enclosing_proc(entry)
        return f"{proc}()" if proc else "(program)"

    def report_stop(self, pc, reason):
        self.stopped_pc = pc
        self.send_event("stopped", {"reason": reason, "threadId": THREAD_ID,
                                    "allThreadsStopped": True})

    # -- request handlers --------------------------------------------------

    def handle(self, req):
        cmd = req["command"]
        handler = getattr(self, "req_" + cmd, None)
        log(f"<- {cmd}")
        if handler is None:
            self.send_response(req, success=True)
            return
        try:
            handler(req)
        except Exception as e:
            log(f"ERROR in {cmd}: {e!r}")
            self.send_response(req, success=False, message=str(e))

    def req_initialize(self, req):
        self.send_response(req, {
            "supportsConfigurationDoneRequest": True,
            "supportsTerminateRequest": True,
            "supportTerminateDebuggee": True,
            "supportsSetVariable": True,
            "supportsEvaluateForHovers": True,
        })

    def req_launch(self, req):
        a = req.get("arguments", {})
        self.cfg = a
        program = os.path.abspath(a["program"])
        self.cwd = a.get("cwd") or os.path.dirname(program)
        box16 = a.get("box16") or DEF_BOX16
        rom = a.get("rom") or DEF_ROM
        xcbasic = a.get("xcbasic") or xcbmap.DEFAULT_XCBASIC
        dasm = a.get("dasm") or xcbmap.DEFAULT_DASM
        target = a.get("target") or "x16"
        for path, what in ((program, "program (.bas)"), (box16, "Box16 fork"),
                           (rom, "rom"), (xcbasic, "xcbasic3 (build the fork)"),
                           (dasm, "dasm")):
            if not os.path.isfile(path):
                raise FileNotFoundError(f"{what} not found: {path}")

        self.output(f"compiling {os.path.basename(program)} (-t {target}) ...")
        self.smap, _, self.prg, summary = xcbmap.generate(
            program, target=target, xcbasic=xcbasic, dasm=dasm)
        self.output(f"source map: {summary}")

        port = int(a.get("port") or 6502)
        host = a.get("host") or "127.0.0.1"
        try:
            socket.create_connection((host, port), timeout=0.3).close()
            raise RuntimeError(
                f"port {port} is already in use -- a Box16 from an earlier "
                "session is probably still running; close it and retry")
        except OSError:
            pass
        cmdline = [box16, "-ignore_ini", "-binarymonitor", "-rom", rom,
                   "-prg", os.path.abspath(self.prg), "-run",
                   "-scale", str(a.get("scale") or 1)]
        self.output("launching Box16 ...")
        self.box16 = subprocess.Popen(cmdline, cwd=os.path.dirname(box16),
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 25
        while True:
            try:
                self.mon = ThreadedMonitor(host, port, self.work)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("Box16 monitor port never opened")
                time.sleep(0.5)
        self.mon.ping()
        # RESET holds the machine paused at the reset vector and re-arms the
        # -prg boot injection, so breakpoints install before code runs.
        self.mon.reset_paused()
        self.launch_req = req
        self.send_event("initialized")

    def req_setBreakpoints(self, req):
        a = req["arguments"]
        path = a["source"]["path"]
        base = os.path.basename(path)
        wanted = [bp.get("line") for bp in a.get("breakpoints", [])]
        for cp in self.breakpoints.pop(path, {}).values():
            try:
                self.mon.checkpoint_delete(cp)
            except Exception as e:
                log(f"checkpoint_delete: {e!r}")
        result, current = [], {}
        for line in wanted:
            entry = (self.smap.line_to_entry(base, line)
                     or self.smap.next_mapped_line(base, line))
            if entry is None:
                result.append({"verified": False, "line": line,
                               "message": "no code for this line"})
                continue
            if entry["line"] not in current:
                current[entry["line"]] = self.mon.checkpoint_set(entry["addr"])
            result.append({"verified": True, "line": entry["line"]})
        self.breakpoints[path] = current
        self.send_response(req, {"breakpoints": result})

    def req_configurationDone(self, req):
        if self.cfg.get("stopOnEntry"):
            first = min(self.smap.entries, key=lambda e: e["addr"])
            self.entry_cp = self.mon.checkpoint_set(first["addr"],
                                                    temporary=True)
        self.send_response(req)
        if self.launch_req is not None:
            self.send_response(self.launch_req)
            self.launch_req = None
        self.mon.resume()
        self.output(f"running {os.path.basename(self.prg)}")

    def req_threads(self, req):
        self.send_response(req, {"threads": [{"id": THREAD_ID, "name": "65C02"}]})

    def req_stackTrace(self, req):
        frames = []
        if self.stopped_pc is not None:
            entry = self.smap.addr_to_entry(self.stopped_pc)
            if entry is not None:
                frames.append({"id": 1, "name": self.frame_name(entry),
                               "line": entry["line"], "column": 1,
                               "source": self.entry_source(entry)})
            else:
                frames.append({"id": 1, "name": f"${self.stopped_pc:04x}",
                               "line": 0, "column": 0})
        self.send_response(req, {"stackFrames": frames,
                                 "totalFrames": len(frames)})

    # -- variables ---------------------------------------------------------

    @staticmethod
    def var_size(v):
        if v["type"] == "string":
            return v["single"] * v["count"]
        return TYPE_WIDTH.get(v["type"], 1) * v["count"]

    def current_var_scopes(self):
        """-> (locals, globals, proc_name). Locals = static variables of the
        SUB/FUNCTION containing the PC; Globals = global/common variables."""
        entry = (self.smap.addr_to_entry(self.stopped_pc)
                 if self.stopped_pc is not None else None)
        proc = self.enclosing_proc(entry)
        globs = [v for v in self.smap.variables if v["scope"] != "local"]
        locs = []
        if proc:
            pfx = proc.lower()
            locs = [v for v in self.smap.variables
                    if v["scope"] == "local" and v["proc"].lower().startswith(pfx)]
        return locs, globs, proc

    def read_var_block(self, variables):
        """Batched reads: -> {id(var): raw bytes}. Merges nearby variables
        into contiguous MEMORY_GET spans (the X16_BasicDebugger lesson)."""
        out = {}
        svars = sorted(variables, key=lambda v: v["addr"])
        i = 0
        while i < len(svars):
            start = svars[i]["addr"]
            end = start + self.var_size(svars[i]) - 1
            j = i + 1
            while j < len(svars):
                a = svars[j]["addr"]
                e = a + self.var_size(svars[j]) - 1
                if a - end <= 32 and e - start < 256:
                    end = max(end, e)
                    j += 1
                else:
                    break
            data = self.mon.memory_get(start, end)
            for k in range(i, j):
                off = svars[k]["addr"] - start
                out[id(svars[k])] = data[off:off + self.var_size(svars[k])]
            i = j
        return out

    @staticmethod
    def _scalar(data, vtype):
        if vtype == "byte":
            n = data[0] if data else 0
            return f"{n} (${n:02x})"
        if vtype == "int":
            return str(int.from_bytes(data[:2], "little", signed=True))
        if vtype == "word":
            n = int.from_bytes(data[:2], "little")
            return f"{n} (${n:04x})"
        if vtype == "long":                # int24, signed 3-byte
            return str(int.from_bytes(data[:3], "little", signed=True))
        if vtype == "float" and len(data) >= 5:   # CBM MFLPT5
            if data[0] == 0:
                return "0.0"
            mant = int.from_bytes(data[1:5], "big") | 0x80000000
            sign = -1 if data[1] & 0x80 else 1
            return repr(sign * mant / 2 ** 32 * 2.0 ** (data[0] - 128))
        return "$" + data.hex()

    @classmethod
    def format_value(cls, v, raw):
        if v["type"] == "string":
            n = raw[0] if raw else 0
            chars = bytes(raw[1:1 + n])
            text = "".join(chr(c) if 32 <= c < 127 else "." for c in chars)
            return json.dumps(text)
        if v["count"] > 1:
            size = TYPE_WIDTH.get(v["type"], 1)
            elems = [cls._scalar(raw[i * size:(i + 1) * size], v["type"]).split()[0]
                     for i in range(min(v["count"], 32))]
            tail = ", ..." if v["count"] > 32 else ""
            return "[" + ", ".join(elems) + tail + "]"
        return cls._scalar(raw, v["type"])

    def req_scopes(self, req):
        locs, globs, proc = self.current_var_scopes()
        self._vars_by_ref = {}
        scopes = []
        if locs:
            self._vars_by_ref[self.LOCALS_REF] = locs
            scopes.append({"name": f"Locals ({proc})",
                           "presentationHint": "locals",
                           "variablesReference": self.LOCALS_REF,
                           "expensive": False})
        if globs:
            self._vars_by_ref[self.GLOBALS_REF] = globs
            scopes.append({"name": "Globals",
                           "variablesReference": self.GLOBALS_REF,
                           "expensive": False})
        self.send_response(req, {"scopes": scopes})

    def req_variables(self, req):
        variables = self._vars_by_ref.get(
            req["arguments"]["variablesReference"], [])
        raws = self.read_var_block(variables)
        body = []
        for v in variables:
            dims = [d for d in v["dims"] if d > 1]
            typ = v["type"] + ("[" + "x".join(map(str, dims)) + "]" if dims else "")
            body.append({"name": v["name"],
                         "value": self.format_value(v, raws[id(v)]),
                         "type": typ,
                         "memoryReference": f"0x{v['addr']:04x}",
                         "variablesReference": 0})
        self.send_response(req, {"variables": body})

    def find_variable(self, ref, name):
        for v in self._vars_by_ref.get(ref, []):
            if v["name"] == name:
                return v
        return None

    @staticmethod
    def parse_int(text):
        t = text.strip().lower()
        if t.startswith("$"):
            return int(t[1:], 16)
        if t.startswith("0x"):
            return int(t, 16)
        if t.startswith("%"):
            return int(t[1:], 2)
        return int(t, 10)

    def req_setVariable(self, req):
        a = req["arguments"]
        v = self.find_variable(a["variablesReference"], a["name"])
        if v is None or v["count"] > 1 or v["type"] in ("string", "float", "dec"):
            raise ValueError(f"cannot set {a['name']}")
        size = TYPE_WIDTH.get(v["type"], 1)
        signed = v["type"] in ("int", "long")
        value = self.parse_int(a["value"])
        data = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")
        self.mon.memory_set(v["addr"], data)
        raw = self.mon.memory_get(v["addr"], v["addr"] + size - 1)
        self.send_response(req, {"value": self.format_value(v, raw)})

    def req_evaluate(self, req):
        a = req["arguments"]
        name = a.get("expression", "").strip().split(".")[-1]
        locs, globs, _ = self.current_var_scopes()
        v = next((x for x in locs if x["name"] == name), None) or \
            next((x for x in globs if x["name"] == name), None)
        if v is None:
            self.send_response(req, success=False,
                               message=f"unknown variable: {name}")
            return
        raw = self.mon.memory_get(v["addr"], v["addr"] + self.var_size(v) - 1)
        self.send_response(req, {"result": self.format_value(v, raw),
                                 "type": v["type"], "variablesReference": 0})

    # -- run control -------------------------------------------------------

    def req_continue(self, req):
        self.stopped_pc = None
        self.send_response(req, {"allThreadsContinued": True})
        self.mon.resume()

    def _current_line(self, pc):
        e = self.smap.addr_to_entry(pc)
        return (e["file"], e["line"]) if e is not None else None

    def _step(self, req, step_over):
        start = self._current_line(self.stopped_pc)
        self.send_response(req)
        pc = self.stopped_pc
        for _ in range(STEP_CAP):
            self.mon.advance(step_over=step_over)
            self.wait_mon(RESP_RESUMED, 10)
            pc = self.wait_stopped(60)
            here = self._current_line(pc)
            if here is None:
                if not step_over:
                    self.mon.until_return()
                    self.wait_mon(RESP_RESUMED, 10)
                    pc = self.wait_stopped(60)
                    here = self._current_line(pc)
                if here is None:
                    continue
            if here != start:
                break
        self.report_stop(pc, "step")

    def req_next(self, req):
        self._step(req, step_over=True)

    def req_stepIn(self, req):
        self._step(req, step_over=False)

    def req_stepOut(self, req):
        self.send_response(req)
        pc = self.stopped_pc
        for _ in range(20):
            self.mon.until_return()
            self.wait_mon(RESP_RESUMED, 10)
            pc = self.wait_stopped(60)
            if self._current_line(pc) is not None:
                break
        self.report_stop(pc, "step")

    def req_pause(self, req):
        self.send_response(req)
        self.mon.checkpoint_set(0x0000, 0xFFFF, temporary=True)

    def req_disconnect(self, req):
        self.shutdown()
        self.send_response(req)
        self.running = False

    def req_terminate(self, req):
        self.shutdown()
        self.send_response(req)
        self.send_event("terminated")

    def shutdown(self):
        if self.mon is not None:
            try:
                for per_file in self.breakpoints.values():
                    for cp in per_file.values():
                        self.mon.checkpoint_delete(cp)
                self.mon.resume()
            except Exception as e:
                log(f"shutdown: {e!r}")
            self.mon.close()
            self.mon = None
        if self.box16 is not None:
            self.box16.terminate()
            try:
                self.box16.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.box16.kill()
            self.box16 = None

    # -- unsolicited stops -------------------------------------------------

    def on_mon_event(self, rtype, err, body):
        if rtype == RESP_STOPPED:
            (pc,) = struct.unpack_from("<H", body, 0)
            reason = "breakpoint"
            if self.entry_cp is not None:
                first = min(self.smap.entries, key=lambda e: e["addr"])
                if pc == first["addr"]:
                    reason = "entry"
                self.entry_cp = None
            self.report_stop(pc, reason)

    # -- main loop ---------------------------------------------------------

    def run(self):
        threading.Thread(target=self.stdin_loop, daemon=True).start()
        while self.running:
            item = self.work.get()
            if item[0] == "dap":
                self.handle(item[1])
            elif item[0] == "mon":
                self.on_mon_event(item[1], item[2], item[3])
            elif item[0] == "dap-eof":
                self.shutdown()
                break


if __name__ == "__main__":
    Adapter().run()
