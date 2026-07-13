#!/usr/bin/env python3
"""XC=BASIC ("XBasic") source-map generator for the X16 debugger.

Builds the `.bas line <-> machine address` map (and a typed variable table)
from a compile with the debug-info fork of xcbasic3
(vinej/xc-basic3 branch debug-info). Unlike the Prog8 map generator, no
reassembly is needed: the patched compiler emits

    ; source: <fileId> <file>:<line>      before each user statement
    ; var: <asmLabel> type=.. single=.. dims=a,b,c vis=N file=X [proc=Y]

and DASM's list file (`-l`) reproduces those comments **with the address
column**, so a single parse of the listing yields line<->address. Variable
addresses come from DASM's symbol dump (`-s`, `V_`/`F_` labels), which also
carries the segment boundaries (library_start bounds user code).

CLI:
    python tools/xcbmap.py <file.bas> [--target x16] [--dump]
        [--xcbasic <exe>] [--dasm <exe>] [-o out.prg]

Also importable: generate(bas, ...) -> (SourceMap, map_path, prg_path, summary)
used by the DAP adapter at launch time (it builds prg + map in one step).
"""

import bisect
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_XCBASIC = os.path.join(ROOT, "xcbasic-sdk", "bin", "Windows",
                               "xcbasic3.exe")
DEFAULT_DASM = os.path.join(ROOT, "dasm-sdk", "dasm.exe")

# vis (visibility) codes emitted by the compiler (Compiler.VIS_*)
VIS_LOCAL, VIS_GLOBAL, VIS_COMMON = 1, 2, 3
SCOPE_NAME = {VIS_LOCAL: "local", VIS_GLOBAL: "global", VIS_COMMON: "common"}

# byte width of each scalar XC=BASIC type (for the debugger's value reader).
TYPE_WIDTH = {"byte": 1, "int": 2, "word": 2, "long": 3, "float": 5,
              "dec": 5, "string": None}  # string width is single (len+1)


class MapError(Exception):
    pass


# -- compile ------------------------------------------------------------------

def compile_bas(bas, prg, target, xcbasic, dasm, extra_flags=None):
    """Invoke the debug-info xcbasic3, producing prg + list + sym next to prg.
    Returns (list_path, sym_path). Raises MapError on compiler failure."""
    base = os.path.splitext(prg)[0]
    lst, sym = base + ".lst", base + ".sym"
    cmd = [xcbasic, bas, prg, "-t", target, "-d", dasm, "-l", lst, "-s", sym]
    if extra_flags:
        cmd += list(extra_flags)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.isfile(prg):
        raise MapError("xcbasic3 failed:\n" + (res.stdout or "") +
                       (res.stderr or ""))
    return lst, sym


# -- parse DASM artifacts -----------------------------------------------------

_ADDR = re.compile(r"[^0-9a-fA-F]*([0-9a-fA-F]{1,4})")
_SRC = re.compile(r";\s*source:\s*(\S+)\s+(.+):(\d+)\s*$")
_VAR = re.compile(r";\s*var:\s*(\S+)\s+(.*)$")


def _list_addr(line):
    """The address column of a DASM list line, or None. Format is
    '<counter> <addr> [bytes] <text>'; the addr may carry a segment-type
    prefix letter (e.g. 'U0b6b' for uninitialized)."""
    parts = line.split(None, 2)
    if len(parts) < 2:
        return None
    m = _ADDR.match(parts[1])
    return int(m.group(1), 16) if m else None


def parse_listing(list_path):
    """-> (entries, var_manifest). entries: [{addr,file,line,fileId,asm_line}]
    from '; source:' comments; var_manifest: [{label, type, single, dims,
    vis, file, proc}] from '; var:' comments."""
    entries, variables = [], []
    with open(list_path, "r", encoding="utf-8", errors="replace") as f:
        for n, raw in enumerate(f, 1):
            ms = _SRC.search(raw)
            if ms:
                addr = _list_addr(raw)
                if addr is not None:
                    entries.append({"addr": addr, "fileId": ms.group(1),
                                    "file": ms.group(2).strip(),
                                    "line": int(ms.group(3)), "asm_line": n})
                continue
            mv = _VAR.search(raw)
            if mv:
                variables.append(_parse_var_manifest(mv.group(1), mv.group(2)))
    return entries, variables


def _parse_var_manifest(label, rest):
    kv = dict(tok.split("=", 1) for tok in rest.split() if "=" in tok)
    dims = [int(x) for x in kv.get("dims", "1,1,1").split(",")]
    return {"label": label, "type": kv.get("type", "int"),
            "single": int(kv.get("single", "0")), "dims": dims,
            "vis": int(kv.get("vis", str(VIS_GLOBAL))),
            "file": kv.get("file", ""), "proc": kv.get("proc", "")}


def parse_symbols(sym_path):
    """DASM symbol dump: '<name> <hexvalue> <flags>'. -> {name: addr}."""
    labels = {}
    with open(sym_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    labels[parts[0]] = int(parts[1], 16)
                except ValueError:
                    pass
    return labels


# -- build the map ------------------------------------------------------------

def _display_name(label):
    """'V_src3.foo' -> 'foo'; 'V_src3.proc.foo' -> 'foo'; 'V_foo' -> 'foo'."""
    name = label[2:] if label[:2] in ("V_", "X_") else label
    return name.rsplit(".", 1)[-1]


def build_variables(manifest, labels):
    out = []
    for v in manifest:
        addr = labels.get(v["label"])
        if addr is None or addr == 0:
            continue  # dynamic/frame-relative locals resolve to 0 -- skip
        count = v["dims"][0] * v["dims"][1] * v["dims"][2]
        out.append({
            "name": _display_name(v["label"]), "label": v["label"],
            "addr": addr, "type": v["type"], "single": v["single"],
            "dims": v["dims"], "count": count, "scope": SCOPE_NAME.get(v["vis"], "global"),
            "file": v["file"], "proc": v["proc"],
        })
    return sorted(out, key=lambda e: e["addr"])


def generate(bas, out=None, target="x16", xcbasic=DEFAULT_XCBASIC,
             dasm=DEFAULT_DASM, prg=None, extra_flags=None):
    """Compile `bas`, build the map, write <base>.xcbmap.json.
    -> (SourceMap, map_path, prg_path, summary). Raises MapError."""
    bas = os.path.abspath(bas)
    if not os.path.isfile(xcbasic):
        raise MapError(f"xcbasic3 not found at {xcbasic} -- build the fork "
                       "(see README Toolchain)")
    base = os.path.splitext(bas)[0]
    prg = prg or (base + ".prg")
    out = out or (base + ".xcbmap.json")

    lst, sym = compile_bas(bas, prg, target, xcbasic, dasm, extra_flags)
    entries, manifest = parse_listing(lst)
    if not entries:
        raise MapError("no '; source:' markers in the listing -- is "
                       "xcbasic3 the debug-info fork build?")
    labels = parse_symbols(sym)
    variables = build_variables(manifest, labels)

    # User code (program + routines) ends at library_start; PCs at/above it
    # (library, KERNAL, ROM) must not map back to a user statement.
    lib = labels.get("library_start")
    code_end = (lib - 1) if lib else (max(e["addr"] for e in entries) + 3)

    data = {"version": 1, "target": target, "bas": bas, "prg": prg,
            "code_end": code_end, "entries": entries, "variables": variables}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)

    files = sorted({e["file"] for e in entries})
    summary = (f"{len(entries)} line entries across {len(files)} file(s), "
               f"{len(variables)} variables, code_end=${code_end:04x}")
    return SourceMap(entries, code_end, variables), out, prg, summary


# -- lookup helper (used by tools + DAP adapter) ------------------------------

class SourceMap:
    def __init__(self, entries, code_end=None, variables=None):
        self.code_end = code_end
        self.variables = variables or []
        self.entries = sorted(entries, key=lambda e: (e["addr"], e["asm_line"]))
        # Several lines can share an address (a FUNCTION header emits no
        # program code, so it collides with the following statement). For
        # PC->line display prefer the LAST entry at an address (the real
        # executable statement rather than a block header).
        self._by_addr = {}
        for e in self.entries:
            self._by_addr[e["addr"]] = e
        self._addrs = sorted(self._by_addr)

    @classmethod
    def load(cls, json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls(d["entries"], d.get("code_end"), d.get("variables"))

    def addr_to_entry(self, pc):
        """Greatest entry with addr <= pc (a statement spans to the next
        mapped one). None if pc is outside the program's own code."""
        if self.code_end is not None and pc > self.code_end:
            return None
        i = bisect.bisect_right(self._addrs, pc) - 1
        return self._by_addr[self._addrs[i]] if i >= 0 else None

    @staticmethod
    def _file_matches(entry_file, suffix):
        a = entry_file.replace("\\", "/").lower()
        b = suffix.replace("\\", "/").lower()
        return a.endswith(b) or b.endswith(a)

    def line_to_addr(self, file_suffix, line):
        e = self.line_to_entry(file_suffix, line)
        return e["addr"] if e else None

    def line_to_entry(self, file_suffix, line):
        for e in self.entries:
            if e["line"] == line and self._file_matches(e["file"], file_suffix):
                return e
        return None

    def next_mapped_line(self, file_suffix, line):
        """Smallest mapped line >= line in that file (breakpoint adjust)."""
        best = None
        for e in self.entries:
            if e["line"] >= line and self._file_matches(e["file"], file_suffix):
                if best is None or e["line"] < best["line"]:
                    best = e
        return best


# -- CLI ----------------------------------------------------------------------

def _main(argv):
    import argparse
    p = argparse.ArgumentParser(description="XC=BASIC source-map generator")
    p.add_argument("bas", help="the .bas source file")
    p.add_argument("--target", default="x16")
    p.add_argument("--xcbasic", default=DEFAULT_XCBASIC)
    p.add_argument("--dasm", default=DEFAULT_DASM)
    p.add_argument("-o", "--out-prg", default=None, help="output .prg path")
    p.add_argument("--dump", action="store_true", help="print the map")
    a = p.parse_args(argv)
    try:
        sm, mp, prg, summary = generate(a.bas, target=a.target,
                                        xcbasic=a.xcbasic, dasm=a.dasm,
                                        prg=a.out_prg)
    except MapError as e:
        print("ERROR:", e, file=sys.stderr)
        return 1
    print(f"map: {mp}")
    print(f"prg: {prg}")
    print(summary)
    if a.dump:
        for e in sm.entries:
            print(f"  ${e['addr']:04x}  {os.path.basename(e['file'])}:{e['line']}")
        for v in sm.variables:
            d = "" if v["count"] == 1 else f"[{'x'.join(map(str, v['dims']))}]"
            print(f"  var {v['name']}{d}: {v['type']} @ ${v['addr']:04x} "
                  f"({v['scope']})")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
