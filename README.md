# gdscript-lsp-bridge

GDScript code intelligence for stdio LSP clients — Claude Code in particular —
by fronting the language server Godot already ships.

Pure Python 3 standard library. No pip, no venv, no Node, no dependencies at
all, now or later.

## What this is, and what it is not

**Godot is the language server.** The GDScript parser, type inference, symbol
index and documentation all live inside the editor binary, which speaks LSP
over a TCP socket when started with `--lsp-port`. That server is complete and
maintained by the Godot team.

The gap is a transport mismatch. Claude Code launches a language server as a
child process and speaks LSP over its **stdio**; Godot listens on **TCP**. So
this project is a byte pipe between the two, plus the process lifecycle
management that makes a warm engine worth reusing.

There is no GDScript parser here. If a symbol resolves incorrectly, that is
Godot's answer being relayed faithfully.

```
Claude Code  ──stdio──▶  bridge.py  ──TCP──▶  godot --editor --headless
                                                    --path ROOT --lsp-port N
```

## Requirements

- Python 3.9 or newer (developed and tested on 3.14.6)
- Godot 4.x on `PATH`, or `GDSCRIPT_LSP_GODOT` pointing at the binary
  (verified against **Godot 4.7.1.stable** on macOS/arm64)

## Install

```sh
git clone <this repo> ~/code/gdscript-lsp-bridge
python3 ~/code/gdscript-lsp-bridge/bridge.py doctor /path/to/your/godot/project
```

`doctor` prints the Godot binary it found, the project root it discovered, the
registry location and the exact engine invocation it would use. Nothing is
installed and nothing is built — pointing a client at `bridge.py` is the whole
deployment.

## Register it with Claude Code

This repository is also a Claude Code plugin marketplace, so installing it is
two commands:

```sh
claude plugin marketplace add ChrisLundquist/gdscript-lsp-bridge
claude plugin install gdscript-lsp@gdscript-lsp-bridge
```

**Then restart.** Language servers are resolved when a session starts, so a
newly installed one is invisible to the session that installed it.

The plugin declares the language server the same way Claude Code's own
`clangd-lsp` and `gopls-lsp` plugins do — an `lspServers` block in
[`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json) naming the
command and the extensions it claims. It resolves `bridge.py` through
`${CLAUDE_PLUGIN_ROOT}`, so no absolute path is written anywhere and nothing
has to be added to a project's own repository.

A project-root `.lsp.json` is **not** a supported registration path; Claude
Code loads language servers from plugins.

No project path is configured either. The bridge reads `rootUri` / `workspaceFolders`
out of the `initialize` request the client already sends, then searches for
`project.godot` at that directory, at its ancestors, and up to two levels
below it. A monorepo whose game lives in `game/` therefore works unconfigured.

## What actually works

Verified end to end against Godot 4.7.1, by the tests in this repository:

| LSP request | Godot 4.7 | Notes |
|---|---|---|
| `textDocument/documentSymbol` | ✅ | Nested, with `detail` and doc comments |
| `textDocument/definition` | ✅ | Resolves across files |
| `textDocument/references` | ✅ | Finds cross-file call sites |
| `textDocument/hover` | ✅ | Renders the `##` doc comment as markdown |
| `textDocument/declaration` | ✅ | |
| `textDocument/completion` | ✅ | With resolve |
| `textDocument/signatureHelp` | ✅ | |
| `textDocument/rename` | ✅ | With prepare |
| `textDocument/documentHighlight` | ✅ | |
| `textDocument/publishDiagnostics` | ✅ | Pushed by Godot |
| **`workspace/symbol`** | ❌ | Godot answers `-32601 Method not found` |
| **call hierarchy** | ❌ | Not implemented by Godot |
| `textDocument/formatting` | ❌ | Not implemented by Godot |

The two ❌ rows that matter for Claude Code are **workspaceSymbol** and **call
hierarchy** — those LSP tool operations will not work for `.gd` files, because
Godot does not implement them. Everything else does. This is a limitation of
the upstream server, not something a bridge can fill in.

## Warm reuse across sessions

Starting a Godot editor costs real time, and that cost is per *project root*,
not per client session. So the engine is deliberately **left running** when the
bridge exits, recorded in a small JSON registry under the OS temp directory, and
reused by the next session against the same root.

Measured on this repository's 3-file test project (macOS, M-series):

| | time to a usable `initialize` |
|---|---|
| Cold, no `.godot` cache | 0.9 s |
| Cold, `.godot` cache present | 0.8 s |
| **Warm reuse** | **0.04 s** |

Those absolute numbers are small because the test project is tiny; a real game
project's cold start is dominated by importing its assets and will be far
larger, which is exactly what reuse avoids paying twice. The 20× ratio is the
part that generalizes.

Concurrent sessions against one root share a single engine — Godot's LSP accepts
multiple clients, and this is covered by a test.

### The registry, and how a project is keyed

`<temp>/gdscript-lsp-bridge/registry.json` maps a project key to `{port, pid}`.

The key is: resolve the physical path (symlinks resolved) → strip the trailing
separator → case-fold → sha256 → first 32 lowercase hex characters.

That scheme is inherited deliberately. The resource two Godot runs contend over
is one `.godot` directory — one regenerable filesystem cache, one import
readiness marker. Two spellings of one worktree must produce one key, or a warm
engine gets missed; two different worktrees must produce different keys, or they
contend over nothing they share. It is the same derivation the author's game
repository uses for its per-worktree engine lock, and the two agree
byte-for-byte on the same root.

Stale entries — dead pid, recycled pid now belonging to another program, or a
port that no longer answers — are detected on lookup, cleaned up, and respawned.

## Managing engines

Because engines outlive the client, they are otherwise invisible:

```sh
python3 bridge.py status              # every recorded engine and its liveness
python3 bridge.py stop [ROOT]         # stop the engine for one project
python3 bridge.py stop-all            # stop every recorded engine
python3 bridge.py reap                # drop entries whose engine is gone
python3 bridge.py doctor [ROOT]       # diagnose a project's setup
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GDSCRIPT_LSP_GODOT` | — | Godot binary to use, overriding `PATH` |
| `GDSCRIPT_LSP_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`, `quiet`. Diagnostics go to **stderr** only; stdout is the protocol channel |
| `GDSCRIPT_LSP_PERSIST` | `1` | `0` makes an engine this session spawned die with the bridge. An engine it merely *reused* is never stopped — it may belong to a live session |
| `GDSCRIPT_LSP_IDLE_TIMEOUT` | `0` | Seconds; when > 0, engines unused for longer are stopped at the next session start |
| `GDSCRIPT_LSP_YIELD_LOCKFILE` | — | See below. Unset means the policy never runs |
| `GDSCRIPT_LSP_YIELD_MODE` | `flock` | `flock` or `exists` |
| `GDSCRIPT_LSP_YIELD_POLL` | `2.0` | Seconds between lock polls |
| `GDSCRIPT_LSP_YIELD_MAX_WAIT` | `1800` | Seconds to wait for a release before relaunching anyway |

## Optional: yielding to another tool's lock

**Off by default.** Nothing in this section runs unless you set the variable.

Some Godot projects serialize engine runs behind a lockfile, because two engines
against one project root fight over its `.godot` cache. A warm LSP engine is
exactly such a run — and a background one, so a validation or export run finds
the slot held by a process the user has forgotten about.

Point the bridge at that lock and it will step aside: stop its engine, wait for
the release, then relaunch and reconnect, replaying the documents the session
had open so the client keeps working.

```sh
export GDSCRIPT_LSP_YIELD_LOCKFILE="$TMPDIR/sr_lock_*.lock"
```

Detection defaults to **attempting the lock**, not checking for the file. The
advisory-lock convention this targets does not unlink its lockfile on release —
the kernel simply drops the lock when the holder's handle closes — so a
file-existence test would report "held" forever after first use. Use
`GDSCRIPT_LSP_YIELD_MODE=exists` only for locks that *do* unlink on release.

No path is hardcoded anywhere. The glob is entirely yours.

What it costs: requests in flight across the swap are lost, and the client is
never told the engine restarted, because LSP has no way to say so. That trade is
why the policy is opt-in.

## Troubleshooting

**"No LSP server available for file type: .gd"** — the client has not picked up
`.lsp.json`. Check the path in `args` is absolute and that `python3` resolves.

**Nothing resolves, no error** — run `python3 bridge.py doctor <root>`. If
`project root: NOT FOUND`, the workspace has no `project.godot` at, above, or
within two levels below it.

**`initialize` fails or times out** — read the engine's own output. Its path is
printed by `doctor` as `engine log:`, and it holds Godot's stdout including
import errors. A project that will not open in the editor will not serve LSP
either.

**Symbols are stale after editing files outside the client** — the engine
indexed the project when it started. `python3 bridge.py stop <root>` forces a
fresh one on the next session.

**A Godot process is running that you did not start** — that is the warm engine,
working as intended. `python3 bridge.py status` identifies it; `stop-all` ends
them all.

**Slow first request on a large project** — a cold engine imports the whole
project before answering. That is the cost warm reuse exists to pay once.

## Tests

```sh
python3 -m unittest discover -s tests -t .          # everything (starts Godot)
GDSCRIPT_LSP_SKIP_ENGINE_TESTS=1 \
  python3 -m unittest discover -s tests -t .        # unit tests only
```

The end-to-end tests drive a real bridge subprocess against a real Godot
against the throwaway project in `test_project/`. They key every engine under a
private `TMPDIR`, so they never read, reuse or stop an engine serving a project
you are working in, and they stop everything they started.

## Design notes

- **Framing is exact.** `Content-Length` counts bytes, not characters, and all
  I/O is binary — a text-mode stream mangling `\r\n` desynchronizes the protocol
  one message later, far from the cause.
- **Traffic is forwarded verbatim.** Only messages the bridge deliberately
  rewrites are re-serialized, so key order and spacing of everything else are
  the peer's own.
- **`shutdown` and `exit` are answered locally, never forwarded.** The engine is
  shared and outlives the session; relaying a client's shutdown would tear down
  a server other sessions are using.
- **Liveness is not "is the pid alive".** A pid can be recycled, and a killed
  child stays signalable as a zombie until reaped — so the check reaps its own
  children, confirms the command line is still a Godot serving this root, and
  confirms the port answers.

## License

MIT. See [LICENSE](LICENSE).
