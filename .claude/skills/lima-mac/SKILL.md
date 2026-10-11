---
name: lima-mac
description: Techniques for managing, debugging, and inspecting Lima macOS VMs in this repo. Use when working with lima.yaml templates, VM builds, TCC databases, disk inspection, configure.sh provisioning, or any lima_mac VM lifecycle task.
---

# Lima macOS VM Management

## Overview

This repo manages Lima macOS guest VMs (`macos-26`, `macos-27`, `macos-15`) used as GitHub Actions runners for blakeports CI. Key knowledge for debugging and maintaining these VMs.

---

## SSH Access to VMs

**Prefer `limactl shell` for running commands** — it's the native Lima interface and consistent with the Makefile. Use direct `ssh`/`scp` only when you need to copy files (virtiofs cache workaround) or when `limactl` itself is unavailable.

Both require **`dangerouslyDisableSandbox: true`** when run via Claude's Bash tool — `limactl` uses Unix sockets and `ssh` accesses identity files outside the sandbox write paths.

### limactl shell (preferred for commands)

```bash
# Interactive shell
limactl shell macos-27

# Run a single command
limactl shell macos-27 -- sw_vers

# Run a script from the virtiofs mount
limactl shell macos-27 -- /Volumes/lima_mac/macports.sh

# Pass env vars
limactl shell macos-27 -- env XCODE_XIP=Xcode_27.xip /Volumes/lima_mac/developertools.sh
```

### Direct SSH (for file copies and virtiofs cache bypass)

Each Lima VM has an SSH config at `~/.lima/<name>/ssh.config`. The host alias is always `lima-<instance-name>`.

```bash
# Copy a file into the VM (bypasses virtiofs cache)
scp -F ~/.lima/macos-27/ssh.config \
    /Users/blake/Developer/lima_mac/macports.sh \
    lima-macos-27:~/macports.sh

# Run the copied file
ssh -F ~/.lima/macos-27/ssh.config lima-macos-27 'bash ~/macports.sh'

# Copy a file out of the VM
scp -F ~/.lima/macos-27/ssh.config \
    lima-macos-27:~/some-log.txt /tmp/some-log.txt
```

### When to use each

| Scenario | Use |
|----------|-----|
| Run a script or command | `limactl shell <name> -- cmd` |
| Pass env vars to a script | `limactl shell <name> -- env VAR=val cmd` |
| Copy a file into the VM | `scp -F ~/.lima/<name>/ssh.config` |
| Script edit not showing via virtiofs | SCP to `~/` then run from there |
| limactl not working / debugging | `ssh -F ~/.lima/<name>/ssh.config lima-<name>` |

---

## Disk Inspection Technique

When a VM is failing provisioning steps, inspect the disk image directly without starting the VM. The disk format is `asif` (Apple sparse copy-on-write), which **cannot** be attached with `hdiutil attach` — use `diskutil image attach` instead.

**Requires `dangerouslyDisableSandbox: true`** — the DiskArbitration framework is blocked by sandbox.

```bash
# 1. Stop the VM first (CI runners may be using it — check with limactl list)
limactl stop macos-26

# 2. Attach the disk image (requires dangerouslyDisableSandbox)
diskutil image attach ~/.lima/macos-26/disk

# 3. Mount the Data volume (noowners bypasses macOS ownership checks)
sudo mkdir -p /Volumes/Data
sudo mount -t apfs -o noowners /dev/diskXsY /Volumes/Data   # find diskXsY from diskutil output

# 4. Inspect contents
# ...see TCC DB, plist, and setup marker sections below...

# 5. Detach when done
sudo hdiutil detach /Volumes/Data
diskutil image detach ~/.lima/macos-26/disk
```

`diskutil image attach` output lists all partitions. Look for the `Apple_APFS` container, then use `diskutil apfs list` to find the Data volume device node.

---

## TCC Database Inspection

TCC (Transparency, Consent, Control) controls macOS privacy permissions. Lima pre-seeds entries via B2 disk patching before first boot.

### Critical: TCC.db cannot be read from a running VM

SIP (System Integrity Protection) blocks all access to TCC databases on a live booted system — even with `sudo`, `sqlite3` returns `authorization denied`. This is true regardless of whether sshd has Full Disk Access pre-seeded. **To inspect TCC entries you must stop the VM and mount the disk directly** (see Disk Inspection Technique above).

```bash
# WRONG — always fails on a running VM:
limactl shell macos-26 -- sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" "SELECT ..."
# Error: unable to open database "...TCC.db": authorization denied

# CORRECT — stop first, then mount disk and query offline:
limactl stop macos-26
diskutil image attach ~/.lima/macos-26/disk
# mount Data volume, then:
sqlite3 /Volumes/Data/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT service, client, client_type, auth_value, auth_reason FROM access ORDER BY service, client;"
```

### Database locations on mounted disk

```bash
# System TCC DB (root-owned services like sshd, guest-agent)
/Volumes/Data/Library/Application\ Support/com.apple.TCC/TCC.db

# User TCC DB (user-specific permissions)
/Volumes/Data/Users/<username>.guest/Library/Application\ Support/com.apple.TCC/TCC.db
```

### Query the DB

```bash
sqlite3 /Volumes/Data/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT service, client, client_type, auth_value, auth_reason FROM access ORDER BY service, client;"
```

### Schema interpretation

| Column | Meaning |
|--------|---------|
| `service` | TCC service, e.g. `kTCCServiceSystemPolicyAllFiles`, `kTCCServiceAccessibility`, `kTCCServiceAppleEvents` |
| `client` | Bundle ID (if `client_type=0`) or absolute path (if `client_type=1`) |
| `client_type` | 0 = bundle ID, 1 = absolute path |
| `auth_value` | 0 = deny, 2 = allow |
| `auth_reason` | 3 = user-set, 4 = admin/system, 9 = denied by policy |

### Expected system TCC entries (B2 disk patching)

```
kTCCServiceSystemPolicyAllFiles | com.openssh.sshd           | bundle | allow | 4
kTCCServiceSystemPolicyAllFiles | com.apple.lima.guest-agent | bundle | allow | 4
kTCCServiceAccessibility        | com.apple.Terminal         | bundle | allow | 4
kTCCServiceAccessibility        | /opt/local/bin/cliclick    | path   | allow | 4
```

**Important**: the `cliclick-accessibility` TCC preset was removed from B2 code. Templates must specify it via custom entry:
```yaml
- service: kTCCServiceAccessibility
  client: /opt/local/bin/cliclick
  clientType: path
  authValue: allow
```

### Checking for bad entries

User TCC DB DENY entries for `kTCCServiceAppleEvents` with `auth_reason=9` indicate macOS wrote them during a failed provisioning run. These appear when:
- The "Update Mac Automatically" dialog blocked Finder/Terminal at first boot
- `sshd-keygen-wrapper` tried to send Apple Events without permission

If you see these, the root cause is usually `suppressFirstLoginSetup` not being configured (see below).

---

## Plist Inspection

```bash
# SetupAssistant plist — controls whether macOS shows express settings dialog
plutil -p /Volumes/Data/Library/Preferences/com.apple.SetupAssistant.managed.plist
# or per-user:
plutil -p /Volumes/Data/Users/<user>.guest/Library/Preferences/com.apple.SetupAssistant.plist

# Expected post-B1 state:
# "MiniBuddyLaunchReason" => 0   (NOT 13 — 13 triggers the "Update Mac Automatically" dialog)
# "SkipExpressSettingsUpdating" => 1

# SoftwareUpdate prefs
plutil -p /Volumes/Data/Library/Preferences/com.apple.SoftwareUpdate.plist
# Expected: "ConfigDataInstall" => 0 (suppresses automatic config data installs)
```

### B1 setup markers on the disk

These files are written by Lima's `Patch()` function during `limactl create` (not at first boot):

```bash
ls /Volumes/Data/private/var/db/.AppleSetupDone     # should exist
ls /Volumes/Data/private/var/db/.skipbuddy           # should exist
ls /Volumes/Data/Library/LaunchDaemons/com.apple.Lima.SetupAssistant.plist  # launch daemon
plutil -p /Volumes/Data/Library/LaunchDaemons/com.apple.Lima.SetupAssistant.plist
```

The `disk.patched` sentinel on the **host** confirms B1+B2 ran:
```bash
cat ~/.lima/macos-26/disk.patched   # exists after successful limactl create
```

---

## suppressFirstLoginSetup — Critical Template Requirement

**All lima.yaml templates MUST have `suppressFirstLoginSetup: {}`** in `vmOpts.vz`. Without it:
1. macOS writes `MiniBuddyLaunchReason: 13` to SetupAssistant plist on first GUI login
2. "Update Mac Automatically" express settings dialog appears at first boot
3. Dialog blocks Finder/Terminal from responding to Apple Events
4. `configure.sh wallpaper` AppleEvent calls time out with `-1712`
5. macOS writes DENY entries to user TCC for `kTCCServiceAppleEvents`

Correct placement in `vmOpts.vz`:
```yaml
vmOpts:
  vz:
    diskImageFormat: "asif"
    rosetta:
      enabled: false
      binfmt: false
    suppressFirstLoginSetup: {}   # <-- REQUIRED, must not be omitted
    guestPatch:
        tccPermissions:
         - preset: sshd-full-disk-access
         - preset: lima-guestagent-full-disk-access
         - preset: terminal-accessibility
         - service: kTCCServiceAccessibility
           client: /opt/local/bin/cliclick
           clientType: path
           authValue: allow
```

This activates B1 fakecloudinit: cidata.go emits `suppress_first_login_setup: true` into user-data; fakecloudinit reads it and calls `suppressFirstLoginScreens()` from `createUser()` on first boot, writing the SetupAssistant plist with `MiniBuddyLaunchReason: 0`.

---

## virtiofs Mount Caching

The `/Volumes/lima_mac/` virtiofs share inside the VM caches files. **Edits made to files on the host are not immediately visible inside the guest.** Do not verify a script edit by re-reading it from `/Volumes/lima_mac/` inside the VM — the guest may still see the old version.

**Workaround**: SCP the file directly to the guest home directory and run it from there:

```bash
scp -F ~/.lima/macos-27/ssh.config \
    /Users/blake/Developer/lima_mac/macports.sh \
    lima-macos-27:~/macports.sh

ssh -F ~/.lima/macos-27/ssh.config lima-macos-27 'bash ~/macports.sh'
```

This bypasses the virtiofs cache entirely. After the VM is restarted the cache clears and the mount reflects the current host state.

---

## Template vs Instance File Sync

There are two copies of each VM config:
- **Template** (source of truth): `/Users/blake/Developer/lima_mac/macos-26.yaml`
- **Instance copy** (live): `~/.lima/macos-26/lima.yaml`

When modifying templates, **also update the instance copy** or the running/next-start VM won't pick up the change. Lima reads from the instance copy.

Active instances: `macos-27`, `macos-26`, `macos-15`.

---

## Serial Log

```bash
# Last boot output — overwritten on each start
cat ~/.lima/macos-26/serialv.log

# Look for:
# - "suppressFirstLoginScreens" (B1 fakecloudinit ran)
# - "createUser: " (guest user creation)
# - Lima provision script output
# - "fakecloudinit" prefixed lines
```

---

## Build Flow (Makefile)

```
limactl create   → installs macOS IPSW + runs B1/B2 disk patch
limactl start    → first boot: fakecloudinit runs, Lima provision scripts run (configure.sh)
limactl stop
limactl start    → second boot (autologin now active)
os-update.sh     → applies macOS updates, reboots if needed
macports.sh      → installs Xcode CLT + MacPorts
autologin-reboot.sh → re-applies autologin if OS update cleared it
wait_mount       → waits for /Volumes/lima_mac virtiofs mount
configure.sh wallpaper  → sets wallpaper; requires Finder + Terminal Apple Events
configure.sh runner     → registers GitHub Actions runner
```

Key make targets: `build-26`, `clean-26`, `rebuild-26` (same pattern for `-27`, `-15` and `-14`).

---

## configure.sh Subcommands

| Invocation | What it does |
|-----------|-------------|
| `./configure.sh` (no arg) | Full first-boot: password, autologin, setup_assistant, screensaver, SSH keys, chezmoi |
| `./configure.sh wallpaper` | Desktop wallpaper (Finder AppleEvents), Terminal AppleEvents pre-approval |
| `./configure.sh runner` | GitHub Actions runner registration (needs `RUNNER_TOKEN`, `RUNNER_LABEL`) |
| `./configure.sh autologin` | Re-applies kcpassword + screensaver after OS upgrade clears autologin |

`configure.sh wallpaper` uses cliclick to click through TCC approval dialogs. It only works in a logged-in GUI session — don't call it over bare SSH without auto-login active and Dock running.

---

## Common Regressions from Patch Refactoring

When refactoring B1/B2 patches in `lima-devl`:

1. **`suppressFirstLoginSetup` not in templates** — most impactful regression; causes "Update Mac Automatically" dialog. Check all three template YAMLs and all instance copies.

2. **`cliclick-accessibility` preset removed** — Lima removed this preset from B2. Templates must use the custom entry form (path-based, not bundle-based). Verify with TCC DB inspection that `kTCCServiceAccessibility | /opt/local/bin/cliclick | type=1 | allow` is present.

3. **Instance copies out of sync** — after editing templates, instance copies in `~/.lima/*/lima.yaml` must be updated manually. The Makefile `rebuild-*` targets do a fresh `limactl create` from the template, but existing instances need manual updates.

4. **B2 TCC entries missing** — if `guestPatch.tccPermissions` section is malformed YAML or missing a required preset, Lima silently skips patching. Inspect system TCC DB after disk attach to confirm all 4 expected entries are present.

5. **`disk.patched` sentinel not created** — if `limactl create` exits with an error mid-way, the sentinel may not exist. The disk may be in a partially patched state. Best fix: `limactl remove -f macos-26` and rebuild.

---

## macports.sh — Known Gotchas

### `set -euo pipefail` + grep exit codes

`grep` returns exit code 1 when it finds no matches. Under `set -euo pipefail`, this kills the script immediately — even inside a variable assignment. The CLT retry loop looks like:

```bash
CMDLINE_TOOLS=$(softwareupdate -l 2>&1 | grep "\*.*Command Line" | ...) || true
```

The `|| true` is **required**. Without it, when softwareupdate hasn't listed CLT yet (returns no matching line), grep exits 1, the script dies, and the retry loop never runs. This was a regression introduced when retries were added without accounting for pipefail.

**Rule**: Any pipeline using `grep` (or `awk`, `sed`, etc.) that may produce no output needs `|| true` when assigned in pipefail scripts.

### softwareupdate timing after macOS installs

After a fresh macOS install or after `softwareupdate --install --all --restart` completes, the softwareupdate daemon can be locked by a post-install daemon for several minutes. During this window, `softwareupdate -l` prints an error like:

```
Scan finished with error: Error Domain=SUMacControllerError Code=7507 
"[SUMacControllerErrorAccessRequestDenied=7507] Access request was denied"
```

**This error is benign** — the download and install still proceed normally. The error is from the listing/scan phase being blocked by a concurrent process, but the actual install request goes through.

The retry loop in `macports.sh` uses 12 attempts × 60s (11 min total) to handle this window. If CLT isn't listed yet on the first attempt, it retries. If CLT is listed but the scan also emits the error, the install still works — `softwareupdate -i "$CMDLINE_TOOLS"` doesn't go through the blocked scan path.

### macOS OS update timing

A typical macOS minor update (e.g., 15.6.1 → 15.7.7) takes roughly 8 minutes to download + install + reboot. `os-update.sh` handles the full cycle:
1. Detects available updates via `softwareupdate -l`
2. Runs `softwareupdate --install --all --restart` (via expect for password)
3. Removes SSH service from bootstrap to force Lima to wait for VM to come back online
4. Waits 2 min then polls for SSH return (up to 15 min)
5. Marks autologin reboot needed

After the OS update, **autologin is cleared by macOS** — `autologin-reboot.sh` re-applies it via kcpassword + screensaver settings and reboots.

---

## Checking Build Progress

```bash
# Monitor a running background build
tail -f /tmp/rebuild-26.log   # or rebuild-15.log

# Screenshot the VM display to see GUI state
limactl screenshot macos-26 /tmp/vm-26.png && open /tmp/vm-26.png

# Check if specific provisioning markers exist in a running VM
limactl shell macos-26 -- ls /var/db/.AppleSetupDone /var/db/.skipbuddy 2>&1
limactl shell macos-26 -- defaults read /Library/Preferences/com.apple.SetupAssistant.managed 2>&1

# Check TCC entries in a running VM (requires Full Disk Access)
limactl shell macos-26 -- sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
  "SELECT service, client, client_type, auth_value FROM access ORDER BY service, client;"
```

---

## MacPorts Patch Fix Workflow

When a blakeports CI run fails due to lint errors in a B1/B2 patch, the fix cycle is:

### 1. Create a working branch and apply the failing patch

```bash
git -C ~/Developer/lima-devl checkout -b b1-lint-fix master
patch -p0 < ~/Developer/blakeports/sysutils/lima-devl/files/patch-05-b1-fakecloudinit.diff
```

If the patch fails on a file that was already modified by an earlier patch (e.g., `lima_yaml.go` fails for B2 because B1 changed alignment), apply the failing hunk manually.

### 2. Fix the issue, then run gofmt

After fixing the code:

```bash
gofmt -w <changed-files>          # normalize ALL changed files
gofmt -l <changed-files>          # must print nothing (clean)
```

**Never skip `gofmt -w` when regenerating patches.** Manually-crafted struct field alignment is almost always wrong. `gofmt` normalizes it deterministically.

### 3. Generate separate B1 and B2 patches using git commits

Because B1 and B2 both modify `lima_yaml.go`, patches must be generated from git commit ranges, not just file diffs:

```bash
# Commit B1 state (after applying B1 and running gofmt)
git -C ~/Developer/lima-devl add -A && git commit -m "temp: B1 state" --no-gpg-sign

# Apply B2 on top (fix any rejects manually, run gofmt)
patch -p0 < ~/Developer/blakeports/sysutils/lima-devl/files/patch-06-b2-tcc.diff
git -C ~/Developer/lima-devl add -A && git commit -m "temp: B2 state" --no-gpg-sign

# Generate patches from commit ranges
git -C ~/Developer/lima-devl diff --no-prefix master HEAD~1 \
  > ~/Developer/blakeports/sysutils/lima-devl/files/patch-05-b1-fakecloudinit.diff

git -C ~/Developer/lima-devl diff --no-prefix HEAD~1 HEAD \
  > ~/Developer/blakeports/sysutils/lima-devl/files/patch-06-b2-tcc.diff
```

### 4. Increment rev and push

```tcl
set b1_rev  5   # increment when patch is regenerated
set b2_rev  5
```

Push blakeports to trigger the CI. Verify no `noctx` or `gci` errors appear.

### B2 patch apply failure — VZOpts alignment

B2 fails its `lima_yaml.go` hunk after a B1 gofmt fix because B2 was generated with the old (unformatted) alignment as context. **Workaround**: apply the failing hunk manually by adding the `GuestPatch VZGuestPatch` field to VZOpts, run `gofmt -w`, then regenerate B2 from the B1 commit as described above.

---

## Upstream PR — Linter Requirements (golangci-lint v2.12.2)

Lima upstream CI runs `golangci-lint` on `ubuntu-24.04`, `windows-2025`, and **`macos-26`** (the strictest — CGo only compiles on darwin). All three must pass before merging.

### Rules that catch most contributors off-guard

| Linter | Rule | Fix |
|--------|------|-----|
| `revive` | `fmt.Errorf("static string")` with no `%` verbs | Use `errors.New("static string")` |
| `unconvert` | `unsafe.Pointer(x)` where `x` is already `unsafe.Pointer` | Drop the cast: `C.free(ptr)` |
| `gci` | Import block formatting | Blank line required between `import "C"` and regular imports; **no inline comments on import lines** (gci strips them, causing a diff). See note below — gci errors often indicate a `gofmt` violation, not an import ordering issue. |
| `gocritic dupImport` | CGo + `unsafe` in same file | See CGo section below |
| `gofmt` / `gofumpt` | General Go formatting | Run `gofmt -w` (then `gofumpt -w`) on ALL changed files before committing or regenerating patches |
| `noctx` | `os/exec.Command` without context | Always use `exec.CommandContext(ctx, ...)` — even for short system queries like `sw_vers`. Functions that call exec must accept a `ctx context.Context` parameter. |
| `nolintlint` | Unused `//nolint` directives | Only add `//nolint` when the linter actually fires |

**Never modify `.golangci.yml`** to work around issues in a PR — upstream will reject it. Fix the code, not the config.

### Diagnosing "File is not properly formatted (gci)"

When golangci-lint reports `gci` at a line number that is clearly NOT in an import block (e.g., line 168 in a 500-line file), the real issue is almost always a `gofmt` violation — over-aligned struct fields or extra spaces in a struct literal. Diagnose with:

```bash
gofmt -l pkg/cidata/cidata.go pkg/limatype/lima_yaml.go   # lists files that would change
gofmt -d pkg/cidata/cidata.go                              # shows exact diff
gofmt -w pkg/cidata/cidata.go                              # apply fix
```

Common root causes from patch regeneration:
- **Extra trailing spaces in struct field alignment**: manually adding spaces to align a longer field (`Rosetta                  Rosetta`) — gofmt uses a specific algorithm and will remove one space
- **Extra alignment in struct literals**: `Param:          instConfig.Param,` — gofmt normalizes key spacing in struct literals to remove hand-added alignment

The fix is always `gofmt -w`, not editing the `.golangci.yml` or adding `//nolint` directives.

### Build tags

Only add `//go:build darwin` (or `darwin && !no_vz`) to files that actually use darwin-specific imports or C code. Command files (`cmd/limactl/`) that merely call a function available on all platforms must **not** have a build tag — or the function becomes undefined on Linux/Windows and `go build ./...` fails.

The pattern used by Lima: put the cross-platform command file without a tag, and put platform-specific implementation files with `_darwin.go` suffix (which Go's file naming applies automatically).

### Transient upstream CI failures — do not chase

These CI jobs in `lima-vm/lima` fail intermittently due to infrastructure issues, not code:

| Job | Cause | Action |
|-----|-------|--------|
| `Lints` | Link checker can't reach `lima-vm.io/docs/` (network error from CI runner) | Ignore — re-run will pass |
| `Windows tests (QEMU)` | TLS handshake timeout fetching Go module zip from `proxy.golang.org` | Ignore — re-run will pass |

Check job logs before chasing: `gh api repos/lima-vm/lima/actions/jobs/<id>/logs | grep -E "error|Error" | head -10`. If the error is a network/TLS failure (not a compile or test failure), it's a flake.

### DCO

Every commit to `lima-vm/lima` requires a `Signed-off-by:` trailer. Use `git commit -s` or `git commit --amend --signoff` to add it.

---

## CGo Patterns — Passing All Linters

When writing CGo code in the `pkg/driver/vz/` package:

### Import structure (required by gci + gocritic)

```go
*/
import "C"

import (
	"errors"
	// other stdlib
)
```

- `import "C"` **must** immediately follow the closing `*/` of the C preamble — no blank line between `*/` and `import "C"`
- A blank line **is required** between `import "C"` and the regular import block (gci enforces this)
- Do **not** add inline comments on import lines — gci rewrites the file without them, causing a format diff

### Avoiding the gocritic dupImport false positive

`gocritic` flags `import "C"` + `import "unsafe"` in the same file as `dupImport` (it treats CGo's pseudo-package as implicitly providing `unsafe`). 

**Fix**: avoid `import "unsafe"` entirely by defining a typed C helper for any `void*` calls:

```c
// In the CGo preamble:
static void freeCString(char *s) { free(s); }
```

```go
// In Go code — no unsafe.Pointer needed:
cuti := C.CString(uti)
defer C.freeCString(cuti)   // ← typed char*, not unsafe.Pointer
```

`C.free(ptr)` (where `ptr` is already `unsafe.Pointer` from a CGo return) does NOT need `unsafe.Pointer(ptr)` — that's the `unconvert` violation. Only `*C.char` → `void*` conversions need a helper.

---

## Driver Optional Interface — Error Architecture

When adding an optional interface to the driver (like `Screenshotter`), follow this pattern so HTTP status codes are set correctly:

**1. Define sentinel errors in `pkg/driver/driver.go`**
```go
var ErrDriverNotScreenshotter = errors.New("driver does not support screenshots")
var ErrNoDisplay = errors.New("no display configured")
```

**2. Wrap at the source with `%w`**
```go
// hostagent.go — driver doesn't implement the interface
return nil, fmt.Errorf("driver %q: %w", name, driver.ErrDriverNotScreenshotter)

// screenshot_darwin.go — display not configured
return nil, fmt.Errorf("%w (set video.display to ...", driver.ErrNoDisplay)
```

**3. Use `errors.Is` in the HTTP server handler**
```go
switch {
case errors.Is(err, driver.ErrDriverNotScreenshotter):
    ec = http.StatusNotImplemented
case errors.Is(err, driver.ErrNoDisplay):
    ec = http.StatusUnprocessableEntity
}
```

**Never use `strings.Contains(err.Error(), "...")` for error classification** — reviewers will ask for `errors.Is`/`errors.As` immediately.

---

## limactl screenshot — Architecture Reference

Added in PR #5098. Useful reference for future display/GUI driver features.

```
limactl screenshot INSTANCE [-o output.png]
  → hostagent client (HTTP GET /v1/screenshot?format=png)
  → hostagent server GetScreenshot handler
  → HostAgent.Screenshot(ctx, format)
  → driver.(Screenshotter).CaptureScreenshot(format)   ← optional interface
  → VZ: captureWindowImageBytes() C/ObjC via AppKit/CoreGraphics
```

- Format is inferred from output extension (`.png` or `.bmp`); any other extension is an error
- HTTP status mapping: `404` = hostagent too old, `501` = driver doesn't support it, `422` = no display configured
- Only VZ drivers with `video.display: default` (or `vz`) implement `Screenshotter`; QEMU returns `501`

---

## Wallpaper / AppleEvents Known Limitation

`configure.sh wallpaper` sets the user desktop wallpaper via `osascript → Finder set desktop picture`. This requires sshd to have `kTCCServiceAppleEvents → Finder` permission. Pre-seeding this in TCC.db via disk patching has been tried; tccd revokes it on first login regardless of `auth_reason`.

**Current behavior**: login window wallpaper succeeds (via `defaults`); user desktop wallpaper is attempted but may be skipped with WARN if AppleEvents times out. This is non-fatal — CI runs are not affected.

The cliclick-based approval in `configure.sh wallpaper` works when auto-login is active and Dock is running (i.e., after a complete build, at the wallpaper Makefile step).
