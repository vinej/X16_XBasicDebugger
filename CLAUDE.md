# X16_XCBasicDebugger — project context for Claude

## What this project is

Source-level debugging of **XC=BASIC 3** ("XBasic") programs (Commander X16
target) in VSCode, via a custom DAP adapter speaking the VICE binary monitor to
the Box16 fork (`vinej/box16`, branch `binary-monitor`). Read `README.md` first
— it carries the charter, the verified feasibility facts, and milestones M0–M5.

**XBasic is a compiler, not an interpreter.** XC=BASIC compiles `.bas` → DASM
assembly → assembles with **DASM** → native `.prg`. So this is a twin of
`X16_Prog8Debugger` (compiled machine code + listing/symbol source map), NOT of
`X16_BasicDebugger` (ROM-interpreter instrumentation). Debug real addresses.

## Relationship to the sibling projects

- `c:\quartus\projects\X16_Prog8Debugger` — **the closest sibling and the reuse
  target.** Its `tools\binmon.py` (VICE binary-monitor client) and its DAP
  adapter (source-map-agnostic monitor/DAP layers) should be reused here with an
  XBasic source-map provider. Don't rebuild the transport.
- `c:\quartus\projects\X16_BasicDebugger` — the BASIC-V2 sibling; proved the
  fork/monitor runtime facts and bank-aware checkpoints.
- `c:\quartus\projects\x16_CDebugger` — the six-toolchain base; hosts the shared
  Box16 fork clone `box16-src` and the emulator/ROM. Fork changes must keep
  `box16-src\test\binmon_test.py` green (six existing debug flows).
- `c:\quartus\projects\X16_XCBasic` — a pristine checkout of upstream XC=BASIC
  v3.2.0 (the source we analyzed). Leave it alone; our patched copy is the fork.

## Verified facts (do not re-derive)

- **Toolchain works end to end for `-t x16`** (2026-07-13): `xcbasic3.exe` +
  `dasm.exe` compile `examples/factorial.bas` to an 876-byte PRG. Loader `$0801`,
  code `$080D`, vars just under `$9EFF`. ZP var window `$35`–`$7F`.
- **The compiler emits NO source-line markers by default** — that is the whole
  reason for the fork. We added `; source: <fileId> <file>:<line>` (before each
  user statement) and `; var: <label> type=… dims=… vis=… file=… [proc=…]`
  (per static variable). Verified live: markers land at correct addresses AND
  **survive into the DASM `-l` list file with the address column**, so the map
  is a single-file parse of the list. `-s` gives label→address for `V_`/`F_`.
- **LF gotcha**: source files are LF-only; the stock compiler counts
  `std.ascii.newline` ("\r\n" on Windows) so its own line numbering is off on
  LF files. Our marker counts `'\n'` — correct. Don't switch it back.
- **Variable addressing**: static globals/locals get fixed addresses
  (`V_<file>.<name>`, `V_<file>.<proc>.<name>`). Dynamic (non-STATIC sub) locals
  show `0000` in the symbol dump — they are frame-relative and need frame-pointer
  resolution (deferred, like Prog8's early scope). FUNCTION headers emit no
  program-segment code, so a header line can share an address with the following
  statement — the map tool must prefer the real statement.
- The DASM list line format is `<counter> <hex-addr> [<bytes>] <text>` (tabs);
  the DASM symbol dump is `<name> <hexvalue> <flags>` (3 columns).

## The compiler fork (vinej/xc-basic3, branch debug-info) — DONE

- Upstream `neilsf/xc-basic3` is public + **MIT** — forking/patching allowed; we
  retain Csaba Fekete's LICENSE and add ours below it.
- **CRITICAL base-version fact**: upstream `main` is v3.1.12 and has **NO x16
  target**. Commander X16 support lives in the `feature/x16-support` line, tagged
  **`v3.2.0-beta`** (af1a5d9). The local `X16_XCBasic` checkout == `v3.2.0-beta`
  byte-for-byte modulo line endings. The debug-info branch is therefore based on
  `v3.2.0-beta`, NOT main. (Upstream also has a `feature/debugging` branch —
  unused by us.)
- **Fork is created and pushed**: `vinej/xc-basic3` branch `debug-info` =
  `v3.2.0-beta` + our 2-file patch (commit a393cab). Cloned into `xcbasic-sdk/`
  (gitignored), with `upstream` remote → neilsf. Patch also saved as
  `docs/debug-info.patch`. No upstream PR (user's choice). Git push works via
  Windows Credential Manager (no `gh auth` needed after all).
- Build: `cd xcbasic-sdk && dub build && cp xcbasic3.exe bin/Windows/`. The exe
  MUST live at `bin/Windows/` because it resolves `lib/` as `<exe>/../../lib`.
  The repo checks out CRLF (autocrlf=true) — the two patched .d files were LF
  normalized before `patch`; git still records only the +26/-1 semantic diff.

## Environment

- **DMD 2.112.0 + DUB 1.41.0** installed via winget at `C:\D\dmd2\windows\bin64`
  (+ `\bin`), both on the **Machine PATH** — new terminals get `dmd`/`dub`
  automatically; already-open ones need a restart. `dub build` fetches `pegged`.
- DASM: `dasm-sdk\dasm.exe` (copied from `x16_CDebugger\dasm-sdk`).
- Emulator: `emulator\box16.exe` (fork) + dlls + `rom.bin` (R48), copied from
  `x16_CDebugger`. Start with `-binarymonitor -ignore_ini`.
- Third-party dirs (`xcbasic-sdk/`, `dasm-sdk/`, `emulator/`) are gitignored.

## User workflow preferences

- GitHub user `vinej`; projects go public there. Commit/push only after the user
  confirms things work (or after Claude-side CLI verification for non-interactive
  pieces).
- Third-party binaries are copied INTO the repo but not committed, with a README
  table telling users where to get them.
- Claude verifies CLI-first; interactive VSCode tests are handed to the user with
  precise steps.
- This repo: published at https://github.com/vinej/X16_XCBasicDebugger (public,
  MIT, branch `master`). Commit/push after the user confirms features work.
  The GitHub repo was created via the REST API using the Git Credential Manager
  token (gh CLI is not authenticated); `git push` works through that credential.

## Status (2026-07-13) — M0–M4 DONE, verified live

Full working debugger. All verified against real Box16 on this machine:

- **M0** — fork `vinej/xc-basic3` branch `debug-info` (base `v3.2.0-beta`),
  patched + pushed + built into `xcbasic-sdk/`. Patch is codegen-neutral
  (patched PRG == stock PRG, byte-for-byte).
- **M1** — `tools/xcbmap.py`: `.bas`→`.xcbmap.json` (line↔addr + typed vars)
  from DASM `-l`/`-s`. No 64tass-style reassembly needed (unlike Prog8) — the
  `; source:` comments already carry addresses in the list.
- **M2** — `tools/step_probe.py`: live line stepping. Uses `reset_paused()` to
  arm the checkpoint before a run-once program starts (the Prog8 probe armed
  after `-run`, only OK for forever-loops).
- **M3/M4** — `tools/dap_adapter.py` + VSCode extension (`type: "xcbasic"`,
  repo root = extension, junctioned into `~/.vscode/extensions`). Compiles on
  launch via `xcbmap`, breakpoints/step/continue/pause, Globals+Locals vars,
  setVariable, evaluate. `tools/binmon.py` copied verbatim from Prog8.
  Regression: `test/dap_smoke.py` (drives the adapter over stdio vs real Box16).

Key gotchas already solved: Box16 needs `emulator/box16-icon56-24.png` or it
quits with "Could not initialize display"; strings read back in **PETSCII**
(uppercase); DIM/FUNCTION-header lines share the first real statement's address.
Next (M5): `INCLUDE` multi-file, dynamic-local frames, decimal formatting,
in-core line stepping for speed.

Adapter trace: env `XCBASIC_DAP_LOG=<file>`. Test program: `examples/demo.bas`.

## x16_library integration — bundled into the fork as modules (2026-07-13)

The `c:\quartus\projects\x16_library` (DASM) is now **bundled into the fork**
(`lib/x16asm/`) and auto-wired for x16: `intermediatecode.d` emits
`INCLUDE "x16.asm"` (top, zero code) + `INCLUDE "x16_code.asm"` (bottom, gated
by `X16_USE_*`). Wrapper modules `lib/x16*.bas` (~27, `SHARED STATIC`, named
`x16_<routine>`) are mostly generated (scratchpad `genwrap.py` parses the
library's `in:`/`out:` comments); `x16ym/x16vera/x16palette` hand-tuned.
`lib/x16const.bas` = `SHARED CONST` enums. A program: `INCLUDE "x16sprite.bas"`
then `CALL x16_sprite_pos(...)`. `examples/bounce.bas` uses this. (The old
`examples/x16lib.bas` paste-in approach is gone.)

Also on `debug-info`: x16 emits `PROCESSOR 65c02` and the 25 runtime `lib/*.asm`
`PROCESSOR` directives are target-aware (DASM allows one processor type; runtime
+ library must agree).

Hard-won XBasic facts (don't re-derive):
- **`SHARED STATIC`** makes a SUB/FUNCTION `COMMON` → callable across `INCLUDE`.
  Plain `STATIC` is module-scoped and a cross-file `CALL` errors "Unknown
  identifier". Same for constants: **`SHARED CONST name = value`** (SHARED
  first) is cross-include visible.
- **Inline asm**: `asm … end asm`, verbatim; `{varname}` → the var's asm
  label/const, so wrappers read STATIC params into A/X/Y/X16_P*. Order stores:
  X16_P* first (they use A), then Y, X, A last.
- **Block IF uses `END IF`** (two words), not `ENDIF`.
- **`VOICE` is a reserved keyword** (sound stmt) — sanitized to `voice_` etc.
- Library ZP at `X16_ZP = $70` (clears XC=BASIC `$22–$34` pseudo-regs); its
  KERNAL regs r0-r15 ($02-$21) don't clash. Keep FAST vars below $70.
- 24-bit fixed point in LONG; `/256` → pixel WORD (expected downcast warning);
  **16-bit constant folds overflow** even into a LONG — compute bounds in LONG
  steps at runtime.
- Headless bounce verification needs a settle delay after `reset_paused` before
  arming the checkpoint, or the large program flakes (server-closed/timeout).
- **16-bit constant overflow**: a constant expression like `(640-16-1)*256`
  (=159488) folds in 16-bit and wraps to 28416 even when assigned to a LONG —
  and for the Y bound it wrapped to a *negative* signed value, freezing motion.
  Fix: compute such values in LONG steps at runtime (`x = 623` then `x = x*256`).
  Debugged live by reading `posx/posy/velx` over frames via the monitor — a good
  worked example of using the debugger itself to find a logic bug.
