# nycu-life macOS / iOS CI runner on Lima (mac-mini)

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
   `security unlock-keychain -p "$MAC_PASSWORD" ~/Library/Keychains/login.keychain-db`
   (works headless; the unlock is visible to the SSH-launched `limactl`). Linux guests are unaffected.
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

## TODO / optimisation
- **Reboot survival (full chain):** host boot → unlock keychain (LaunchDaemon) + `limactl start macos-ci`;
  guest auto-login → runner as a LaunchAgent/service. (Runner currently started via `run.sh`.)
- **Hygiene:** runner cleans the per-job workspace; for deeper isolation, periodically re-clone the
  golden guest from a pristine snapshot (`limactl clone` is a full ~39 GB copy — not per-job viable).
- **Signing/TestFlight:** import the signing cert per build; add the App Store Connect API key secret.
- Org-level runner via the GitHub App; verify a full iOS *test* job (simulator boot) under the service.
