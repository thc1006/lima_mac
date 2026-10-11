# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Skill

Use the `/lima-mac` skill (`.claude/skills/lima-mac/SKILL.md`) when working on VM builds, debugging provisioning failures, inspecting TCC databases, or any Lima VM lifecycle task. It captures techniques that are not obvious from reading the code: disk mounting, TCC DB schema, common regressions, `suppressFirstLoginSetup` requirement, and configure.sh subcommand structure.

## What this repo does

Lima configurations and shell scripts for running macOS guest VMs on Apple Silicon. Four VM targets are maintained: `macos-27`, `macos-26` (Tahoe), `macos-15` (Sequoia N-2), and `macos-14` (Sonoma N-3). The VMs serve as GitHub Actions runners for [blakeports](https://github.com/trodemaster/blakeports) CI.

## Common commands

```bash
make status                  # Show all Lima instance states
make build-26                # Full build: create, provision, OS update, MacPorts, autologin reboot, wallpaper, register runner
make clean-26                # Deregister runner, force-stop, and remove VM
make rebuild-26              # clean then build (same pattern for -27, -15 and -14)
SKIP_OS_UPDATE=1 make rebuild-15  # Skip OS update check (speeds up test builds)

# Check login state inside a running VM
limactl shell macos-15 -- bash -c 'who && pgrep -x Dock && defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser'

# Get the guest IP (port forwarding is not automatic)
limactl shell macos-15 ipconfig getifaddr en0
```

Override tool paths: `make build-26 LIMACTL=/opt/local/bin/limactl`

## Architecture

**Entry point**: `Makefile` — all VM lifecycle operations go through here. Each VM has three targets: `build-<ver>`, `clean-<ver>`, `rebuild-<ver>`. No global all-VMs target (Apple limits concurrent macOS VMs to 2, and two simultaneous cold-starts collide at the VZ layer — stagger them).

**VM configs** (`macos-27.yaml`, `macos-26.yaml`, `macos-15.yaml`, `macos-14.yaml`): Lima YAML files specifying the IPSW image URL, resources (cpus/memory/disk), virtiofs mounts, and provisioning scripts. All VMs mount `~/Developer/lima_mac` into the guest at `/Volumes/lima_mac`. The YAML provisioning scripts write `/etc/sudoers.d/admin` granting `%admin ALL=(ALL) NOPASSWD:ALL` at first boot.

**`configure.sh`** — runs inside the VM at first boot via Lima's provisioning system, and also called explicitly by the Makefile for specific sub-commands:
- *(no arg)*: full first-boot provisioning — password setup, auto-login, setup assistant suppression, screensaver/lock/energy-saver disable, SSH key injection, chezmoi bootstrap
- `runner`: registers the VM as a GitHub Actions runner (called with `RUNNER_LABEL` and `RUNNER_TOKEN` env vars)
- `autologin`: re-applies auto-login after OS upgrades clear it — also re-runs `configure_screensaver` to restore lock/energy-saver settings
- `wallpaper`: sets login window and user desktop wallpaper to Space Gray; skips user desktop if Finder doesn't respond (non-fatal)

**`os-update.sh`** (host-side) — called by `build-*` after the second `limactl start`. Checks `softwareupdate -l` for available updates; if restart-requiring updates are found, SCPs `os-update-guest.sh` into the VM, runs it via SSH (foreground), sleeps 2 minutes for the reboot to fire, then polls for the VM to come back online. Writes `~/.needs-autologin-reboot` marker on the guest after a successful OS update reboot.

**`os-update-guest.sh`** (guest-side, SCP'd to `/tmp/os-update-guest.sh`) — runs inside the VM via SSH. Uses `expect` to handle any password prompts from `softwareupdate --install --all --restart`. If the update requires a restart, calls `sudo launchctl bootout system/com.openssh.sshd` after softwareupdate exits to immediately drop SSH and force the host-side wait loop to wait through the full reboot cycle.

**`scripts/autologin-reboot.sh`** (host-side) — called by `build-*` after `macports.sh`. Checks for `~/.needs-autologin-reboot` marker on the guest (written by `os-update.sh`). If present: deletes the marker, runs `configure.sh autologin` (re-applies kcpassword + screensaver/lock settings), reboots the VM, and waits for SSH to return. No-ops if the marker is absent (no OS update occurred).

**`scripts/wait-online.sh`** — polls `limactl shell $INSTANCE -- true` every 15s (up to 15 min) until the VM is reachable via SSH after a reboot.

**`macports.sh`** — run explicitly during `build-*`. Installs Xcode CLT (headless via `softwareupdate` with trigger file; retries up to 6× since the update daemon may be busy after an OS update reboot), MacPorts (binary PKG from GitHub API if available, otherwise source build), configures fcix/MIT archive mirrors, and runs `port selfupdate`. Does not touch `sources.conf` — that is managed at CI runtime by `blakeports/scripts/installmacports`.

**Makefile `wait_mount` macro** — called after `autologin-reboot.sh` before the wallpaper step. Polls until `/Volumes/lima_mac/configure.sh` exists inside the guest (up to 2 min, 5s intervals). The Lima guest agent creates the virtiofs symlink a few seconds after SSH is reachable; without this wait the wallpaper step fails with "no such file or directory."

**`.envrc`** (gitignored, copy from `example.envrc`): secrets sourced by `configure.sh` inside the VM via the shared volume. Key variables: `MACOS_PASSWORD`, `SKIP_CHEZMOI`, `SSH_PUBLIC_KEY`, `AUTO_LOGIN`.

**Runner registration**: the `runner` sub-command of `configure.sh` is called by the Makefile with `RUNNER_TOKEN` generated on the host via `gh api`. The token is never stored in the VM — it is passed as a short-lived env var only. Runner registration is always the last step so the runner cannot pick up jobs during a reboot.

## Scripting constraints

**No Python in VM-side scripts** — the guest VMs are intentionally minimal and do not have Python (or other non-default developer tools) installed. All scripts that run inside the VM (`configure.sh`, `os-update-guest.sh`, `macports.sh`, and anything SCP'd into the guest) must use only tools that ship with a stock macOS install: `bash`, `PlistBuddy` (`/usr/libexec/PlistBuddy`), `plutil`, `defaults`, `awk`, `sed`, `curl`, `scp`, `expect`, `launchctl`, `softwareupdate`, etc. `jq` is NOT available until MacPorts is installed. Do not add `python3`, `ruby`, `perl`, or any port/brew dependency to guest-side scripts.

## Key constraints and caveats

**Passwordless sudo** — the YAML provisioning scripts write `/etc/sudoers.d/admin` at first boot granting passwordless sudo to all admin users. Guest-side scripts can use plain `sudo` without password injection. The guest user's password is also stored in plaintext at `~/password` (e.g. `/Users/blake.guest/password`) for the rare case where it's needed (e.g. expect scripts that must handle an interactive prompt).

**macOS 26 requires `Lima.app` bundle** — macOS 26 (Tahoe) crashes `limactl` with `SIGTRAP` if not running inside a registered `.app` bundle. Install Lima via MacPorts; `Lima.app` is placed at `/Applications/MacPorts/Lima.app` automatically. Plain `brew install lima` does not work for macOS 26 guests.

**`DEGRADED` status is cosmetic** — `limactl list` shows `DEGRADED` because `/run` is read-only on macOS guests and Lima's SSH auth socket link fails. The VM is fully functional.

**No automatic port forwarding** — macOS guests do not support Lima's port forwarding protocol. Use the vzNAT IP directly or `ssh -L` tunnels.

**`xcode-select --install` cannot be used headlessly** — it opens a GUI dialog. `macports.sh` uses `softwareupdate -l` + `softwareupdate -i` instead, with a trigger file (`/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress`) to make CLT appear in the catalog.

**softwareupdate busy after OS update reboot** — the `softwareupdate` daemon runs post-update tasks for several minutes after a restart. `macports.sh` retries the CLT listing step up to 6 times (60s apart) to ride out this window.

**Auto-login is cleared by OS upgrades** — macOS point-release upgrades reset auto-login. `configure.sh autologin` must be called after the OS update and before the final reboot to re-apply it. The `build-*` Makefile targets do this automatically via `scripts/autologin-reboot.sh`.

**Auto-login fallback** — `sysadminctl -autologin set` silently fails on macOS 15+ when run from SSH (no Security Agent context). `configure.sh` detects this and falls back to writing `/etc/kcpassword` directly using bash + `xxd` with the XOR cipher algorithm.

**Screen lock after auto-login** — macOS 15 defaults "Require password after screen saver begins or display is turned off" to Immediately. Even with the screensaver disabled (`idleTime=0`), the display lock fires when Lima.app connects to show the virtual display after a reboot. The fix is `defaults write com.apple.screensaver askForPassword -int 0` (both regular and `-currentHost` domains). `configure_screensaver` handles this and is called from both the full provisioning run and the `autologin` subcommand.

**Energy saver settings** — `configure_screensaver` also disables Power Nap (`pmset -a powernap 0`), standby, all sleep timers, and App Nap (`NSAppSleepDisabled`). These are inappropriate for a CI runner VM.

**Analytics dialog on macOS 26+** — a "Share Mac Analytics with Apple" consent dialog appears on first login even with `.AppleSetupDone` present. `configure_setup_assistant` suppresses it by writing to `/Library/Application Support/CrashReporter/DiagnosticMessagesHistory.plist`.

**Wallpaper: Finder AppleEvent timeout** — `configure.sh wallpaper` uses `osascript` to set the user desktop wallpaper via Finder. This times out when run over SSH because the SSH daemon (`sshd-keygen-wrapper`) lacks TCC permission to control Finder via Apple Events (`kTCCServiceAppleEvents`). The login window wallpaper is set successfully (via `defaults`); only the user desktop wallpaper is skipped (non-fatal WARN). Pre-seeding `kTCCServiceAppleEvents` via TCC.db disk patching has been attempted across user DB / system DB / multiple `auth_reason` values — all revoked by tccd at first login. See `docs/TCC_DB.md` for the iteration history and `docs/MACOS_SECURITY.md` for the full constraint analysis.

**Parallel VM cold-start collision** — two macOS VMs cannot be started (created + booted) simultaneously; the second one fails with `VZErrorDomain Code=1 "The virtual machine failed to start."` Two VMs can run concurrently once both are started. Stagger builds: start the first VM and wait for it to reach READY before starting the second.

**virtiofs mount timing** — after a reboot, the Lima guest agent creates the `/Volumes/lima_mac` symlink a few seconds after SSH becomes reachable. The Makefile `wait_mount` macro polls for this before running the wallpaper and runner steps.

**`softwareupdate --restart` is used in os-update-guest.sh** — the `--restart` flag is passed to `softwareupdate --install --all --restart` inside the VM. After it exits, `launchctl bootout system/com.openssh.sshd` is called to drop SSH immediately, forcing the host-side wait loop to properly wait through the full reboot. Do not remove the `bootout` call or the host may detect the VM as "back online" before it actually reboots.

**`sources.conf` is intentionally untouched by `macports.sh`** — it is configured at CI workflow runtime by `blakeports/scripts/installmacports` to point at the runner workspace checkout.

**macOS 27 beta: `defaults write` can hang boot indefinitely (pre-release flakiness, not root-caused)** — observed on a macOS 27 beta guest: `configure_setup_assistant`/`configure_screensaver`'s per-user `defaults write`/`pmset`/`plutil` calls round-trip through cfprefsd, and one blocked forever (near-zero CPU, not spinning) while Setup Assistant itself was separately stuck at the first-boot progress screen (`Setup Assistant -MiniBuddyYes`, also near-zero CPU). Not seen on macOS 26 or 15. Since this is pre-release Apple software, `configure.sh` doesn't try to fix the underlying hang — `run_with_timeout` wraps both functions' call sites (60s cap, non-fatal) so a stuck cfprefsd round-trip can't block `configure.sh`, and therefore Lima's "boot scripts must have finished" gate (~10 min budget), forever. A killed call may leave a harmless orphaned `defaults`/`pmset` process behind. If the guest's GUI session itself is stuck (not just one script), the deeper hang is un-fixable from configure.sh — recreate the VM (`make rebuild-27`) and treat it as beta flakiness, consistent with `TODO.md`'s existing note that macOS 27 fakecloudinit/DFU workarounds (`M1`/`B3` patches in `lima-devl`) are beta-only and not upstreamed.

## Prerequisites (host)

- Apple Silicon Mac, macOS 15+
- Lima 2.1+ installed via MacPorts (`sudo port install lima`) — not Homebrew
- `Lima.app` at `/Applications/MacPorts/Lima.app`
- `gh` CLI authenticated (`gh auth status`)
- `jq`
- `blakeports` checked out at `~/Developer/blakeports` (for `ghrunner` script)
