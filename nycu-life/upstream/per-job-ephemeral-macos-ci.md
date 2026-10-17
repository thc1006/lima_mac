# Per-job ephemeral macOS CI with Lima CoW clones (reference recipe)

A working pattern for a **self-hosted GitHub Actions macOS/iOS runner** where **each job runs in a
fresh, throwaway VM** — satisfying "no persistent runner identity or secrets, clean environment per
job" on a single Apple-silicon host. Built on Lima (`vz`) macOS guests; validated end-to-end
(Swift build+test + iOS Simulator boot) on a 16 GB M4 Mac mini.

Shareable with lima-vm/lima (usage docs) and trodemaster/lima_mac. No secrets included.

## The model
```
clean GOLDEN (stopped)         Xcode + simulators only — NO runner registration, NO Apple ID
   │                           session, NO signing keys. Pinned Lima/IPSW/Xcode versions.
   │  per queued job:
   ├─ limactl clone  ──────────▶  CoW clone (instant, ~0 disk — see finding #4)
   │   unlock host keychain        (headless; see lima-macos-guest findings #1)
   │   limactl start clone
   │   JIT ephemeral runner  ───▶  generate-jitconfig + `run.sh --jitconfig …` → one job, auto-removes
   │   (job runs: swift/xcodebuild, simctl boot — works headless in the clone, no GUI session)
   └─ limactl stop+delete clone ▶  environment destroyed; golden untouched
```
Why clone-per-job (not a persistent runner in the golden): a stopped clone copies the disk incl.
any runner credentials/keychain/secrets, and VZ snapshots aren't implemented — so the only way to
guarantee a clean per-job environment is a clean golden + fresh clone + discard. A persistent runner
would leave identity/secrets that every clone inherits.

Reference implementation: [`mac-ci-supervisor.sh`](../mac-ci-supervisor.sh) — a host loop with
admission control (pause on recent HID input / low disk), GitHub polling, clone/JIT/run/destroy,
and startup reconcile of orphan clones.

## Gotchas worth upstreaming / documenting

### A. The supervisor's job poll must scan NON-completed runs, not just `status=queued`
In a multi-job workflow run, other jobs (e.g. Linux) make the **run** `in_progress` while the macOS
job is still `queued` waiting for the label. Polling only `status=queued` **runs** silently strands
those macOS jobs. Correct: list runs with `status != completed` and look for jobs with
`status=queued` whose `.labels` include your runner label.

### B. Injecting the Apple ID 2FA code into `xcodes` in a headless guest
`xcodes install <ver>` needs Apple ID + an interactive 2FA code. In a headless guest:
- `screen -X stuff "<code>\n"` does **not** reach xcodes's secure prompt read (tried CR and LF).
- **Works:** drive `xcodes` under `expect`, read the code from a FIFO, `send` it:
  ```
  expect { -re "digit code" { set fh [open /tmp/otp r]; send -- "[gets $fh]\r"; exp_continue } eof }
  ```
  The operator does `echo <code> > /tmp/otp`. One-time per golden build.
- xcodes's final step (6/6) prints its own `macOS User Password:` prompt (not `sudo`, so NOPASSWD
  sudo doesn't satisfy it). Simplest: let xcodes place `Xcode-<ver>.app` in `/Applications`, then
  finish manually with passwordless sudo: `xcode-select -s …`, `xcodebuild -license accept`,
  `xcodebuild -runFirstLaunch`. Install the Simulator runtime with `xcodebuild -downloadPlatform iOS`.

### C. CoreSimulator works in the clone's job context without a GUI session
A full iOS job (`swift test` + `xcrun simctl create/boot/...`) succeeds inside the CoW clone when
run via `limactl shell … -- run.sh` (an SSH user context). A logged-in Aqua session was **not**
required for simulator *boot* in our tests (Xcode 26.6 / iOS 26.5 sim, M4). Signing/UI-test lanes
may still differ and should be validated separately.

### D. JIT runners are the clean fit
`POST /repos/{o}/{r}/actions/runners/generate-jitconfig` + `run.sh --jitconfig <cfg>` gives a
single-use runner that auto-removes its own `.credentials`/`.runner` on exit — no registration
token lifecycle to manage, and nothing left in the (discarded) clone.

## Operational notes (not upstream, but for operators)
- Use a **scoped, non-expiring PAT** for the supervisor (Actions:read + Administration:read/write on
  the repo), not a user OAuth token (broad + it expires). A `repo`+`workflow` OAuth token also works
  for `generate-jitconfig` if you must — but it expires and is over-broad.
- **Reboot survival (verified here by 2× real `shutdown -r now`):** run the supervisor under a launchd
  **LaunchAgent** (`RunAtLoad` + `KeepAlive`). Auto-login brings up the `gui/$UID` session that loads
  the agent; the supervisor unlocks the login keychain **in-process** (per security-session — see
  guest-findings #1), so it does **not** depend on the GUI login having unlocked the keychain.
- **Admission control right after a reboot:** immediately after auto-login the HID idle counter reads
  ≈ uptime, so a naive "pause while HID idle < N" check **falsely pauses CI for the first N seconds**.
  Gate it on `uptime > N` as well — and parse the boot epoch with
  `sysctl -n kern.boottime | awk '{print $4}'`, NOT a greedy `sed 's/.*sec = .../'` (that grabs the
  `usec`).
- **Shared-host RAM:** another auto-starting heavy VM will fight the macOS guest (a 16 GB host runs
  **one** at a time). In particular, a Lima instance can auto-start at boot via a root
  `/Library/LaunchDaemons/io.lima-vm.daemon.<name>.plist` (`RunAtLoad`, `limactl start <name>
  --foreground`) that **`limactl start-at-login --enabled=false` does not manage** — the CLI reports
  "not registered" because it only touches the per-user login agent, a different mechanism. Disable the
  boot daemon with `sudo launchctl bootout system/io.lima-vm.daemon.<name>` and remove/move the plist.
- Pin everything: Lima version, the IPSW build, the Xcode version. Rebuild the golden on updates.

*Validated 2026-10-18: Lima v2.2.0, macOS 26.5 guest, Xcode 26.6 (17F113), iOS 26.5 Simulator.*
