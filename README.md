# X16 XBasic Debugger

**Goal: source-level debugging of [XC=BASIC 3](https://xc-basic.net) ("XBasic")
programs on the Commander X16 in VSCode** — breakpoints on `.bas` lines, step
over/into/out, inspect variables — using the emulator infrastructure already
proven by [x16_CDebugger](https://github.com/vinej/x16_CDebugger) (six
toolchains), [X16_BasicDebugger](https://github.com/vinej/X16_BasicDebugger),
and [X16_Prog8Debugger](https://github.com/vinej/X16_Prog8Debugger):

```
VSCode ──DAP──► custom debug adapter ──VICE binary monitor (TCP 6502)──► Box16 fork
```

Status: **feasibility proven, keystone landed** (2026-07-13). The enabling
compiler change is done and verified live (see M0 below); the source-map tool
and DAP adapter are next.

## Why XBasic is a *compiled* target (not like X16 BASIC V2)

XC=BASIC is **not** an interpreted BASIC. It is a cross-compiler: it turns
`.bas` source into MOS 6502/65C02 assembly and assembles it with **DASM** into
a native `.prg`. So this project is architecturally a twin of the **Prog8**
debugger (source → machine code + a symbol/listing-based source map), *not* the
BASIC-V2 debugger (which instruments the ROM interpreter). There is no
interpreter to hook — we debug real machine code at real addresses.

The toolchain, verified live for `-t x16`:

```
factorial.bas ──xcbasic3.exe──► (intermediate DASM asm) ──dasm.exe──► factorial.prg
                                          │                      │
                                    -k keeps it            -l list file (addr↔asm)
                                                           -s symbol dump (label↔addr)
```

A BASIC-loader stub sits at `$0801`; program code starts at `$080D`; variables
occupy an uninitialized segment just below the top address (`$9EFF` on X16).

## The source map — the one thing XC=BASIC didn't already give us

A source-level debugger needs `.bas line ↔ machine address`. XC=BASIC hands us
most of the pieces for free, and we added the missing one:

| Need | Source | Status |
|---|---|---|
| label/variable ↔ address | DASM symbol dump (`-s`) — `V_<file>.<name>`, `F_<file>.<proc>` | ✅ built in |
| asm line ↔ address | DASM list file (`-l`) | ✅ built in |
| **asm ↔ `.bas` line** | **`; source: <fileId> <file>:<line>` markers** | ✅ **added** (see below) |
| variable **types**/dims/scope | **`; var: <label> type=… dims=… vis=… file=…` manifest** | ✅ **added** |

Unlike `prog8c` (which emits `; source: file:NN` by default), stock XC=BASIC
puts **no** source-line markers in its assembly. Rather than a fragile
preprocessor, we fork the (MIT-licensed) compiler and add the markers at the
source — a ~30-line change in two files. The markers **survive into the DASM
list file with their addresses**, so the map is a single-file parse:

```
    42  0826    ; source: src3 factorial.bas:1     <- line 1 lives at $0826
    43  0826    ; source: src3 factorial.bas:5
    49  084d    ; source: src3 factorial.bas:6     <- PRINT is $084D
    70  08e3    ; source: src3 factorial.bas:7     <- NEXT  is $08E3
    86  08fb    ; source: src3 factorial.bas:2     <- IF (inside the FUNCTION) $08FB
```

### The compiler fork

The patched compiler is **[vinej/xc-basic3](https://github.com/vinej/xc-basic3),
branch `debug-info`** — a fork of upstream
[neilsf/xc-basic3](https://github.com/neilsf/xc-basic3) (MIT), based on tag
**`v3.2.0-beta`**. This base matters: upstream `main` is v3.1.12 and has **no
X16 target at all** — Commander X16 support first appears in the
`feature/x16-support` work that became `v3.2.0-beta`. The branch is cloned into
`xcbasic-sdk/` (gitignored) and built locally with DMD/DUB. The two-file change
is preserved in [docs/debug-info.patch](docs/debug-info.patch):

* `source/compiler/compiler.d` — before each **user** statement, emit
  `; source: <fileId> <file>:<line>` into the program/routine segment. Line
  number counts `'\n'` (LF-safe; the stock compiler's `std.ascii.newline` count
  misreports on LF files under Windows).
* `source/compiler/variable.d` — when a static variable reserves storage, emit
  `; var: <asmLabel> type=… single=… dims=… vis=… file=… [proc=…]` into the
  VARIABLES segment.

Upstream's MIT license is retained in the fork; our copyright is added below
Csaba Fekete's. No upstream PR is planned — the fork is self-contained, exactly
like the Box16 fork.

## X16 specifics (from the XC=BASIC source, `-t x16`)

| Fact | Value |
|---|---|
| BASIC loader / start address | `$0801` (with `--basic-loader`, default) |
| Program code start | `$080D` |
| Default top address | `$9EFF` |
| Zero-page variable window (`FAST`) | `$35`–`$7F` |
| Variable label form | `V_<fileId>.<name>` (global), `V_<fileId>.<proc>.<name>` (local), `V_<name>` (COMMON); `X_…` for compiler-private |
| Types | `byte`, `int`, `word`, `long` (int24, 3 bytes), `float` (MFLPT), `dec`, `string` (len-prefixed), UDTs; arrays up to 3 dims |

User code runs in **low RAM**, so plain 16-bit exec checkpoints suffice (like
Prog8) — the Box16 fork's bank-aware checkpoint extension is available but not
needed unless code moves into banked RAM. **Static** globals/locals have fixed
addresses (easy to read); **dynamic** locals (non-`STATIC` sub frames) live at a
frame offset (`address 0000` in the symbol dump) and need frame-pointer
resolution — deferred to a later milestone, same as Prog8's early scope.

## Milestones

- [x] **M0 — enable + prove the source map.** Fork XC=BASIC, add the
  `; source:` / `; var:` debug hooks, rebuild with DMD/DUB, and confirm the
  markers land at correct addresses in the DASM list. **Done 2026-07-13**:
  `factorial.bas -t x16` produces markers for lines 1,2,3,5,6,7 at
  `$0826/$08fb/$09e7/$0826/$084d/$08e3`; the `; var:` manifest typed
  `V_src3.i` as `long` global and the function return value as `long` local.
- [x] **M1 — source-map generator** (`tools/xcbmap.py`). **Done**: compiles
  with `-l -s`, parses the DASM list (track `; source:` → address) + symbol
  dump (`V_`/`F_` labels, `library_start` = `code_end`) + `; var:` manifest →
  `<name>.xcbmap.json` with `line ↔ address` and typed variable records.
  Verified on `factorial.bas` (6 lines, 2 vars) and `demo.bas` (14 lines,
  4 typed vars). Prefers the real statement when several lines share an address
  (DIM/FUNCTION headers reserve storage but emit no code).
- [x] **M2 — proof of stepping** (`tools/step_probe.py`). **Done**: reuses the
  Prog8 `binmon.py` transport; resets Box16 to a paused state so the checkpoint
  arms *before* the (run-once) program starts, hits the line, maps PC→line,
  steps until the line changes. Verified live: `demo.bas:14` → `$0880`, steps to
  line 15.
- [x] **M3 — the DAP adapter + VSCode extension** (`type: "xcbasic"`).
  **Done**: `tools/dap_adapter.py` compiles the `.bas` via `xcbmap` on launch
  (no separate build task), launches the Box16 fork, attaches over the binary
  monitor, and serves line breakpoints (auto-adjusted to the next statement),
  step over/into/out, continue, pause, stop-on-entry, and PC→line highlight.
  The repo root is the extension (`package.json` + `extension.js`), with `.bas`
  syntax highlighting (`syntaxes/xcbasic.tmLanguage.json`) and keyword
  completions. Verified headlessly by `test/dap_smoke.py` against real Box16.
- [x] **M4 — variables**. **Done**: Globals + per-SUB/FUNCTION Locals panes
  from the `; var:` manifest + `-s` addresses, formatted by type
  (byte/int/word/long, MFLPT float, PETSCII length-prefixed string, arrays),
  batched MEMORY_GET reads, hover **evaluate** and **setVariable**. Verified in
  `test/dap_smoke.py` (`total` long, `count` byte, `msg` string, `i` loop var).
- [ ] **M5 — polish**: multi-file (`INCLUDE`) programs, dynamic-local frame
  resolution (non-STATIC sub locals), decimal-type formatting, and the fork's
  in-core line stepping for speed.

## Using it

The extension is installed via an NTFS junction into
`%USERPROFILE%\.vscode\extensions\vinej.x16-xbasic-debug-0.1.0` → this repo, so
edits to `tools\dap_adapter.py` apply on VSCode restart. Open this folder in
VSCode, open `examples\demo.bas`, set gutter breakpoints, and press **F5**
(configs in `.vscode\launch.json`). The adapter needs **Python 3** on PATH and
the built `xcbasic-sdk\bin\Windows\xcbasic3.exe`. Trace with
`XCBASIC_DAP_LOG=<file>`. CLI checks without VSCode:

```
python tools\xcbmap.py examples\demo.bas --dump    # M1: source map
python tools\step_probe.py --line 14               # M2: live stepping
python test\dap_smoke.py                            # M3+M4: full session
```

## Toolchain (gitignored — copied in, get them here)

| Folder | What | Where to get it |
|---|---|---|
| `xcbasic-sdk/` | Patched XC=BASIC compiler (fork of `v3.2.0-beta`) + `lib/`, built with DMD/DUB | `git clone -b debug-info https://github.com/vinej/xc-basic3 xcbasic-sdk`; `dub build`; copy the exe to `xcbasic-sdk/bin/Windows/` (it resolves `../../lib`) |
| `dasm-sdk/` | `dasm.exe` (the assembler XC=BASIC calls) | <https://github.com/dasm-assembler/dasm/releases> |
| `emulator/` | Box16 fork (`box16.exe` + `SDL2.dll` + `zlibwapi.dll` + `icons.png` + `box16-icon56-24.png`) and `rom.bin` | Build [vinej/box16 branch `binary-monitor`](https://github.com/vinej/box16/tree/binary-monitor); ROM from [x16-emulator releases](https://github.com/X16Community/x16-emulator/releases). **Note:** `box16-icon56-24.png` is required — Box16 quits with "Could not initialize display" if it is missing. |

**Build prerequisite:** the compiler fork needs **DMD 2.11x + DUB** (installed on
this machine at `C:\D\dmd2\windows\bin64`, on the Machine PATH). Rebuild with:

```
cd xcbasic-sdk && dub build && cp xcbasic3.exe bin/Windows/xcbasic3.exe
```

## License

MIT — see [LICENSE](LICENSE). The bundled XC=BASIC fork keeps its own upstream
MIT license (© Csaba Fekete).

## References

- [XC=BASIC 3 docs](https://xc-basic.net/doku.php?id=v3:start) and upstream
  [neilsf/xc-basic3](https://github.com/neilsf/xc-basic3).
- [X16_Prog8Debugger](https://github.com/vinej/X16_Prog8Debugger) — the closest
  sibling (compiler → machine code + listing map); its `binmon.py` and DAP
  adapter are the reuse targets here.
- [X16_BasicDebugger](https://github.com/vinej/X16_BasicDebugger) — the
  BASIC-V2 sibling; proved the fork/monitor runtime facts.
- [vinej/box16 `binary-monitor`](https://github.com/vinej/box16/tree/binary-monitor)
  — the emulator fork; protocol notes in x16_CDebugger's `debugger.md`.
