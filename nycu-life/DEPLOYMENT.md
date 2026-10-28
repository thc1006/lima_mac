# nycu-life macOS / iOS CI runner on Lima (mac-mini)

Pre-cutover image maintenance uses the opt-in
[failure recovery boundary](maintenance-recovery.md) to restore original runner
admission when a phase fails and safe recovery is proven. A recovered failed
phase keeps its nonzero exit code; no automatic live installation is implied.

The separate [coordinated source-pair installation draft](supervisor-install.md)
is plan-only and has not been deployed. It preserves rollback failures and does
not reuse unchanged-PID maintenance proofs across a service restart.

The repository supervisor also pauses new admission when the native disk probe
fails, its GiB output is malformed or ambiguous, or `MIN_FREE_GB` is invalid.
It checks again after the GitHub queue lookup, before cloning or unlocking.
The default 15 GiB threshold is unchanged. This is source behavior until the
reviewed supervisor is installed; it neither changes GitHub job conclusions
nor replaces the maintenance recovery boundary. Offline failure tests are in
`nycu-life/tests/test_supervisor_disk_admission.py`.

Self-hosted GitHub Actions **macOS/iOS** runner for `github.com/nycu-life/super-app`,
built on a **Lima** (CNCF) macOS guest on an Apple **M4 / 16 GB** Mac mini.

Status: **working end-to-end** — a real GitHub Actions job (`runs-on: nycu-life-macos`) ran on
it and passed (Xcode 26.6 + iOS 26.5 Simulator verified). This doc records the recipe and the
(many) gotchas so it can be re-built, maintained, and optimised.

> This is the **macOS/iOS half**. Linux CI runs on ARC/k3s (`nycu-life-arm64`, idle backup);
> the real Linux jobs run on the org's existing `nycu-life-linux`. Android's x86_64 emulator
> needs an x86_64 + KVM host (4060 / broadwell) — not this Mac.

## Why Lima
- Lima is **CNCF incubating** and already runs the host's Linux k3s.
- Lima v2.1+ runs **macOS guests** via Apple's Virtualization.framework (`vz`).
- No CNCF tool virtualises macOS directly (KubeVirt = Linux/KVM only); `vz` is the only path.
- `agoda-com/macOS-vz-kubelet` (Virtual Kubelet) was validated as a k8s-native alternative but is
  experimental; Lima chosen for maturity.

## Architecture
```
macOS host (26.5 / 25F71, M4, 16 GB, FileVault OFF)
└─ Lima vz  ─ instance "macos-ci"  (macOS 26.5 / 25F71 guest)
      ├─ Xcode 26.6 (17F113)  +  iOS 26.5 Simulator runtime (23F77)
      └─ GitHub Actions runner "macos-ci" — label: nycu-life-macos
```
16 GB ⇒ run **one heavy VM at a time** (the macOS guest OR the Linux k3s, never both under load).

## Gotchas (read before touching anything)
1. **host macOS must be ≥ guest macOS.** Otherwise the guest fails to boot with
   `VZErrorDomain Code=-9 "security error" → "Failed to create new HostKey" (POSIX 22)`.
   The stock `template:macos-26` ships 26.5.2 (25F84), NEWER than our 25F71 host → fails.
   Fix: pin the guest to a host-matching IPSW (25F71). See `macos-ci.yaml`.
2. **Headless/SSH leaves the login keychain LOCKED, and `vz` needs it UNLOCKED** to create the
   guest's HostKey — same `Code=-9` error even when host==guest. THE real blocker (not version).
   Fix: unlock it. Either a GUI/auto-login session, or **over SSH**:
   `security unlock-keychain ~/Library/Keychains/login.keychain-db`
   and enter the password at its native prompt. For unattended supervisor use,
   `unlock-keychain.py` answers that prompt through a private PTY; no password is
   passed in process arguments or environment variables.
   (works headless; the unlock is visible to the SSH-launched `limactl`). Linux guests are unaffected.
   **The unlock is per security *session*** — do it in the SAME process that runs `limactl start`
   (the supervisor does, right before cloning). A *separate* SSH `security show-keychain-info` may
   still print "User interaction is not allowed" even though the guest boots fine — that check is
   misleading; don't gate on it. A GUI login is NOT required for the keychain (auto-login is only
   needed to load the LaunchAgent). Proven: from a locked keychain, in-session unlock → clone →
   `limactl start` boots the guest in ~8 s with no `Code=-9`.
3. **SIGTRAP on macOS-26 guests is fixed in Lima ≥ 2.2.0** (upstream PR #5036, thread-pinning).
   You do NOT need MacPorts `Lima.app`.
4. **Auto-login (reboot survival):** `sysadminctl -autologin set` fails `error:22` on macOS 26.
   Write `/etc/kcpassword` (XOR-obfuscated) + `defaults write /Library/Preferences/com.apple.loginwindow
   autoLoginUser <user>` directly. Plus `pmset -a sleep 0 disablesleep 1` and screensaver lock off.
5. **Inside the guest:** it boots to loginwindow with NO user session and NO login keychain.
   Before `xcodes` (Apple ID), **create + unlock a login keychain** from Lima's generated password
   (`~/password`), and give the guest user **passwordless sudo** (its sudo needs a password otherwise).
6. **Xcode via `xcodes` needs Apple ID + interactive 2FA.** `screen -X stuff` does NOT reach xcodes's
   secure read. Use **`expect` + a FIFO**: expect spawns xcodes; on the 2FA prompt it reads the code
   from a FIFO you `echo` into. One-time per golden-image build.
7. **xcodes step (6/6) prompts `macOS User Password:`** (its own prompt, not `sudo`). Easiest: let it
   place `Xcode-26.6.0.app` in `/Applications`, then finish manually with passwordless sudo:
   `xcode-select -s …`, `xcodebuild -license accept`, `xcodebuild -runFirstLaunch`.
8. The workflow pins **`/Applications/Xcode_26.6.app`** (underscore); xcodes installs
   `Xcode-26.6.0.app`. **Symlink** `Xcode_26.6.app → Xcode-26.6.0.app`.
9. **iOS Simulator runtime is not bundled:** `sudo xcodebuild -downloadPlatform iOS` (~8.5 GB, 26.5).
10. **Disk:** the guest instance is ~39 GB; the IPSW download cache (~18 GB each) is only needed to
    CREATE the guest — delete `~/Library/Caches/lima/download/*` afterwards to reclaim it.

## Deploy (ordered; run from the host over SSH, `PATH=/opt/homebrew/bin`)
1. **Host:** Lima ≥ 2.2.0, FileVault off, auto-login + `pmset` (`scripts/host-setup.sh`).
2. **Unlock host keychain** (gotcha #2).
3. **Create guest:** `limactl start --name=macos-ci --cpus=4 --memory=8 --disk=80 nycu-life/macos-ci.yaml`.
4. **Provision guest** (`scripts/guest-provision.sh`): guest keychain + passwordless sudo; `xcodes`;
   install Xcode 26.6 via `expect`+FIFO (feed the 2FA); finish (xcode-select/license/runFirstLaunch);
   symlink `Xcode_26.6.app`; `xcodebuild -downloadPlatform iOS`.
5. **Runner:** download `actions-runner-osx-arm64`; `./config.sh --url …/nycu-life/super-app
   --token <RUNNER_REG_TOKEN> --labels nycu-life-macos --work _work --unattended`; start with `run.sh`.
   - Repo-level token via `gh api -X POST repos/nycu-life/super-app/actions/runners/registration-token`.
   - Org-level needs the `nycu-life-action` GitHub App (`organization_self_hosted_runners`).
6. **Wire super-app:** change `ios-native` / `ios-ipad-ui` (and `mobile-release.ios`)
   `runs-on: macos-26` → `nycu-life-macos` (PR, not main).

## Secrets (NEVER commit — all referenced as placeholders above)
- Mac login/sudo password (auto-login `/etc/kcpassword`, host keychain unlock).
- Apple ID + password (xcodes Xcode download; one-time).
- App Store Connect API key (only the `mobile-release` TestFlight lane).
- GitHub App private key / runner registration tokens.

## Status — per-job ephemeral clone (DONE 2026-10-18)
Hardened to **per-job ephemeral clones** (see `mac-ci-supervisor.sh` and
`upstream/per-job-ephemeral-macos-ci.md`):
- [x] Clean golden (Xcode-only; NO persistent runner identity, NO Apple ID session, NO secrets).
- [x] `limactl clone` is **APFS CoW — instant, ~0 disk** (an earlier "39 GB full copy" assumption was wrong).
- [x] Supervisor: detect queued job → CoW clone → JIT ephemeral runner → one job → destroy clone;
      admission control (pause on recent HID input / low disk) + startup reconcile of orphan clones.
- [x] Verified end-to-end: a full iOS job (`swift test` + simulator **boot**) ran inside a spawned
      clone, then the clone was destroyed; the golden stayed untouched.

## Reboot survival — launchd LaunchAgent (DONE 2026-10-18)
The supervisor runs under a user **LaunchAgent** (`com.nycu-life.mac-ci-supervisor.plist` in this
dir), `RunAtLoad` + `KeepAlive`, logging to `~/ci/supervisor.log`. Auto-login (gotcha #4) brings up
the `gui/$UID` session that loads the agent; the supervisor then unlocks the keychain in-process
(gotcha #2), so the keychain does **not** depend on the GUI login.
```sh
cp nycu-life/com.nycu-life.mac-ci-supervisor.plist ~/Library/LaunchAgents/
U=$(id -u)
launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist
launchctl enable    gui/$U/com.nycu-life.mac-ci-supervisor
# Drain an installed service before restarting it; see the procedure below.
launchctl print    gui/$U/com.nycu-life.mac-ci-supervisor | grep -E 'state =|pid ='
```
Install `unlock-keychain.py` beside `~/ci/mac-ci-supervisor.sh` when replacing the
supervisor at a drained, idle boundary. It requires the host's existing Python 3
and uses only its standard library. Keep `~/ci/.kc_pw` a regular, owner-only file
(mode `600` or `400`, one link, at most 1024 bytes); symlinks, changed metadata,
embedded terminal control characters and empty passwords fail closed. The helper
allows one prompt response within 15 seconds, discards native output and reports
only a generic failure. It inherits the supervisor's security audit session and
does not change keychain settings, the default keychain or the search list.

The supervisor calls `unlock_keychain` once at startup (logs `[startup] keychain unlocked`) and again
before each clone, so it is CI-ready immediately after boot, not only when the first job lands.

### Maintenance: drain before restarting

Request a pause with `touch ~/ci/maintenance.drain`. The supervisor checks this
before admitting another job and again after the GitHub lookup, before cloning.
An already-admitted job continues through runner completion and clone cleanup.
Observe a newly appended `[admission] drain — idle` line after the request
(not an old match in `supervisor.log`) **and** confirm that no `macos-job-*` instance
remains before stopping or restarting the service. The file check is not an atomic
lock with clone creation; the presence of the request alone is not proof of idle.

The request survives a host reboot. At startup it blocks reconciliation, keychain
unlock and dispatch until removed. This prevents `RunAtLoad`/`KeepAlive` from
resuming work before the upgraded host has been verified. Keep the existing golden
stopped, check SSH and the intended toolchain, stop any candidate VM, then remove
the request with `rm ~/ci/maintenance.drain` to resume. `DRAIN_FILE` follows the
supervisor's existing environment-default convention for isolated test instances.

The first deployment of this gate requires special care because the old process
does not read the new script. Create the drain request first for the replacement
script; its presence does not pause the old process. At an idle boundary, send `SIGSTOP` to only the
supervisor service using `launchctl kill SIGSTOP gui/$(id -u)/com.nycu-life.mac-ci-supervisor`,
then inspect that PID's process group and recheck the VM list over several polls.
Use only `pid,ppid,pgid,stat,comm` fields when inspecting processes: full command
arguments can expose the encoded JIT configuration. An in-flight provisioning
child can create a VM after the parent stops. Require no live children other than
`sleep` and no job clone; if a clone appeared, send `SIGCONT` and let it finish.
Only after confirming idle should you `launchctl disable` the service and `bootout`.
Confirm the service is unloaded and its old process group has exited. Install the
reviewed script by writing a new file and atomically renaming it while unloaded;
retain the old script and plist. Run `plutil -lint` on the plist. Use `launchctl enable`
**before** `bootstrap`, with the drain request still present, then confirm new
`startup paused` lines and no reconciliation or dispatch. Never use `kickstart -k`
or restart the host to interrupt a busy runner: startup reconciliation deletes
job clones. If drain cannot finish, inspect the runner; a JIT process waiting for
a job has no timeout in the existing supervisor and must not be assumed idle.
Keep the maintenance window below GitHub's 24-hour queued-job limit.

For a toolchain canary, use an instance name outside `macos-job-*` and a label
that does not include `nycu-life-macos`. A compound label containing that default
label is seen by the production supervisor and can occupy its only VM slot.
Keep production drained during the canary and run one heavy macOS VM at a time.
The golden image is CI-image rollback, not a verified backup of the physical host.

The drain loop can be exercised without real VMs or GitHub credentials on a POSIX
host: `python3 nycu-life/tests/test_supervisor_drain.py`. The fixture runs the
actual script with disposable command stubs; it does not change the live service.
The private-input helper has separate offline PTY fault tests:
`python3 nycu-life/tests/test_unlock_keychain.py`. These use a synthetic password
and mocked native CLI; they do not establish native macOS keychain acceptance.

**Reboot-survival test — PASSED (two full `shutdown -r now` cycles, 2026-10-18):** SSH back in
~35–42 s; `console_user=thc1006` (auto-login fired), LaunchAgent `state=running` at boot, log shows
`[startup] keychain unlocked`, golden `macos-ci` intact/Stopped, zero orphan clones.
Two issues the 1st reboot surfaced, fixed and **re-verified clean by the 2nd reboot**:
- (a) for ~5 min post-boot the HID idle counter reads ~uptime, so admission briefly paused as "human
  active" — fixed by gating `human_active` on uptime > `IDLE_SECS` (parse the boot epoch with
  `sysctl -n kern.boottime | awk '{print $4}'`, NOT `sed 's/.*sec = .../'` which greedily grabs the
  `usec`). 2nd reboot: `idle=50s uptime=58s → human_active=FALSE`, no false pause.
- (b) `k3s`/`k3s-agent1` auto-started via **system LaunchDaemons**
  `/Library/LaunchDaemons/io.lima-vm.daemon.k3s{,-agent1}.plist` (root, `RunAtLoad`,
  `limactl start --foreground`) and came back Running (~8 GB), colliding with the 8 GB macOS CI guest
  on the 16 GB host. Note `limactl start-at-login --enabled=false` does **not** touch these (different
  mechanism — it reports "not registered"). Fix: `sudo launchctl bootout
  system/io.lima-vm.daemon.k3s{,-agent1}` + moved the plists to `~/ci/disabled-launchdaemons/`
  (reversible: move back + `sudo launchctl bootstrap system <plist>`). 2nd reboot confirmed k3s stays
  Stopped. The arm64 ARC backup is meant to stay stopped.

### Remaining
- **Scoped PAT (hardening, not a blocker):** the current `~/ci/.gh_token` is the user's `gho_` OAuth
  token (scopes `repo, workflow, read:org`); it was **proven able to `generate-jitconfig`** on
  super-app, so the runner works now. Replace it with a non-expiring fine-grained PAT
  (Actions:read + Administration:read/write on the repo) to be least-privilege and non-expiring.
- **Wire super-app:** draft PR **nycu-life/super-app#105** opened — `ci.yml` `ios-native` +
  `ios-ipad-ui` `runs-on: macos-26` → `nycu-life-macos` (release lane `mobile-release.ios` left on
  `macos-26` until signing is set up). The PR's own `pull_request` run exercises the runner end-to-end;
  confirmed the supervisor auto-picks-up the real `Native iOS` job into a per-job clone. Team reviews/merges.
- **Signing/TestFlight:** per-job temp keychain + App Store Connect API key (release lane only).
- Org-level runner via the `nycu-life-action` GitHub App.

## 2026-10-19 reliability hardening (merge-night root causes)

Re-apply all three when rebuilding the golden/supervisor.

### 1. lima macOS-guest boot race — `/var/run/lima-boot-done` never written (THE clone-start blocker)
`cidata` is a cd9660 ISO macOS auto-mounts ASYNCHRONOUSLY (DiskArbitration). The one-shot LaunchDaemon
`io.lima-vm.lima-macos-init` (`RunAtLoad`, no `KeepAlive`) ran `/usr/local/sbin/lima-macos-init.sh`,
which only checked the `/Volumes/cidata` DIRECTORY then `exec`d `lima-guestagent`. On a fast clone boot
it won the race vs the mount -> `No such file or directory` -> the one-shot daemon died, never retried
-> `boot.sh` never ran -> marker never written -> `limactl start` hung on "boot scripts must have
finished". Clone-start success was ~12%.

FIX (inside the golden guest, as root; then stop the golden so clones inherit it):
- Rewrite `/usr/local/sbin/lima-macos-init.sh` to bounded-wait for the real binary:
```sh
#!/bin/sh
set -eux
date
n=0
until [ -x /Volumes/cidata/lima-guestagent ]; do
  n=$((n+1)); [ "$n" -ge 60 ] && { echo "cidata/lima-guestagent not available after 60s" >&2; exit 1; }
  sleep 1
done
exec /Volumes/cidata/lima-guestagent fake-cloud-init
```
- Add self-heal to `/Library/LaunchDaemons/io.lima-vm.lima-macos-init.plist`:
  `<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>`

After the fix clone-start is ~100% (~10s). Raising the golden cpu makes clones boot FASTER and lose the
race MORE, so only raise cpu after this fix is in.

### 2. supervisor zombie-clone death-spiral
`cleanup`/`reconcile` used `limactl stop`/`delete` WITHOUT `-f`; a timed-out start left a half-running VM
that bare `delete` refuses to remove -> zombie `macos-job-*` clones piled up, ate RAM (2x10GiB -> host
swap-thrash), starved the next start -> deadlock. FIX: `-f` on every `limactl stop`/`delete` (now in
mac-ci-supervisor.sh) + `limactl start --timeout 90s` so a lost boot-race fails fast and retries.

### 3. golden resources
`cpus: 8`, `memory: 10GiB` in the golden `~/.lima/macos-ci/lima.yaml` (safe once fix #1 is in).
