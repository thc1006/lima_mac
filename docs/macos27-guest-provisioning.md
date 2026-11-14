# macOS 27 Guest Provisioning (`VZMacGuestProvisioningOptions`)

Research + design notes for the planned upstream PR that enables Apple's native
macOS-guest first-boot provisioning. This is the **macOS 27+ successor** to the
fake-cloud-init work merged in **PR #5336** (`osOpts.Darwin.suppressFirstLoginSetup`,
Issue #5186). Fake cloud-init stays the path for macOS ≤ 26 guests; this feature
takes over when host **and** guest are both macOS 27+.

Companion doc: [ipsw-build-manifest.md](ipsw-build-manifest.md) — the guest-OS-version
detection this feature depends on (the "PR 1" prerequisite).

Status as of 2026-11-14: **working end to end** in lima-devl P1 r9, on blakeports
main (`db495c6`). The account keeps the host user name and the guest agent moves its
home to `/Users/<host user>.guest`; see "2026-11-13 Moving the home" below. Local
only: P1 runs on Code-Hex/vz v4 main (port patch Z1, no v4 tag yet). The first
working build was r4 on 2026-10-05, on a vz fork; see "2026-10-05 Working end to
end" for findings and the Apple-vs-Lima mismatches.

---

## API surface (from Apple docs + the Xcode 27.0 SDK headers)

Verified two ways on 2026-09-10:
- Apple doc JSON: `developer.apple.com/tutorials/data/documentation/virtualization/vzmacguestprovisioningoptions.json`
- SDK headers on this host: `MacOSX27.0.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZMacGuestProvisioningOptions.h` (Xcode 27.0 is installed even though the host OS is 26.6.2)

### `VZMacGuestProvisioningOptions` — `API_AVAILABLE(macos(27.0))`, `#ifdef __arm64__`

Inherits `VZGuestProvisioningOptions`, conforms to `NSCopying` / `NSSecureCoding` /
`Sendable`. Unlike the base class (whose `init` / `new` are `NS_UNAVAILABLE`),
**`- (instancetype)init;` is available** — you instantiate the subclass directly.

| Property | Type | Meaning |
|----------|------|---------|
| `fullName` | `NSString *` (readwrite, copy) | Account full name |
| `username` | `NSString *` (readwrite, copy) | Login username |
| `password` | `NSString *` (readwrite, copy) | Account password |
| `logsInAutomatically` | `BOOL` (readwrite) | Auto-login at startup |
| `enablesRemoteLogin` | `BOOL` (readwrite) | Enable Remote Login / SSH. Defaults to `NO`. |

Base class `VZGuestProvisioningOptions` provides `- (BOOL)validateWithError:(NSError **)error`.

### Attaching it — `VZMacOSVirtualMachineStartOptions` (exists since `macos(13.0)`)

- `@property (readonly, nullable, copy) VZMacGuestProvisioningOptions *guestProvisioningOptions API_AVAILABLE(macos(27.0));`
- `- (BOOL)setGuestProvisioningOptions:(nullable VZMacGuestProvisioningOptions *)options error:(NSError **)error API_AVAILABLE(macos(27.0));`
  - Swift: `func setGuestProvisioning(_:) throws`
  - **Validates and sets** — the property is read-only; this method is the setter and the validation entry point.
- Also carries `startUpFromMacOSRecovery: BOOL`.
- The start-options object is passed to `-[VZVirtualMachine startWithOptions:completionHandler:]`.

### Error codes (`VZError.h`, `API_AVAILABLE(macos(27.0))`)

| Code | Symbol |
|------|--------|
| `40001` | `VZErrorGuestProvisioningInvalidFullName` |
| `40002` | `VZErrorGuestProvisioningInvalidUsername` |
| `40003` | `VZErrorGuestProvisioningInvalidPassword` |

`setGuestProvisioningOptions:error:` is where these surface. The PR should map them
to friendly messages naming which field is bad.

---

## Behavioral constraints (verbatim from Apple)

> "This configuration requires guest macOS 27 or later to function properly. Earlier
> versions of macOS don't support the automated guest configuration protocol and
> **ignore these settings**."

> "macOS only evaluates these options **on the first boot after restore**. The
> Virtualization framework can't use them to reconfigure macOS once the framework
> has already provisioned it."

> "Changes to the properties after starting the virtual machine have no effect."

Consequences:

1. **One shot, first boot only.** The options must be attached to the start-options
   for the *very first* `VZVirtualMachine.start()` after `VZMacOSInstaller` finishes.
   There is no reapply.
2. **≤ 26 guest silently ignores it.** No error, no signal — the account just
   doesn't get created. So a guest-version check is mandatory *before* choosing
   this path; you cannot rely on a runtime failure.
3. **Set before start.** Mutating the options object after `start()` is a no-op.

---

## Runtime availability on the host

Probed the live framework on macOS 26.6.2 (25G83) on 2026-09-10 via the ObjC runtime:

```
VZMacGuestProvisioningOptions      -> absent   (objc_getClass returns nil)
VZGuestProvisioningOptions         -> absent
VZMacOSVirtualMachineStartOptions  -> present  (old class; the new members are 27.0-gated)
```

So on a ≤ 26 host the class literally does not exist at runtime — the code path
must be guarded by runtime ObjC lookup (`NSClassFromString` / `objc_getClass`),
not just a compile-time `@available`, and skipped entirely with a warning.

---

## Existing implementation — branch `feat/vz-mac-guest-provisioning`

Local branch in `~/Developer/lima-devl`, commit `b2fcc2dd` (2026-07-30), ~540 lines.
On the `trodemaster` fork. **Predates** the current design decisions below — treat
it as a starting point, not final.

What it already does:
- New YAML: `vmOpts.vz.guestProvisioning` — `fullName`, `username`, `password`,
  `logsInAutomatically`, `enablesRemoteLogin` (`VZGuestProvisioning` struct in
  `pkg/limatype/lima_yaml.go`).
- CGo/ObjC: `pkg/driver/vz/macos_guest_provisioning_darwin_arm64.{go,h,m}` — all
  **runtime ObjC dispatch**, so it compiles against any SDK. `guestProvisioningAvailable()`
  returns false on macOS < 27.
- Integration: a transient "provisioning boot" runs after `installMacOS` completes;
  macOS applies the settings on that boot; Lima then stops the VM so its normal
  `Start()` is a clean second boot with the account already configured.
- `guestProvisioningAvailable()` false → step skipped, non-fatal, `configure.sh`
  handles setup as usual.

What it does **not** do yet (the gap this PR closes):
- Still `vmOpts.vz.*` — must move to `osOpts.Darwin.*` (see below).
- No guest-version check — only gates on the host. A 27 host + 26 guest would try
  the native path and silently get nothing.
- No downlevel-host warning — just silently no-ops on a ≤ 26 host.
- Rebase needed (branch is from 2026-07-30).
- `configure.sh` overlap (password / autologin / SSH remote login) noted in the
  commit message, not resolved.

---

## Design decisions (2026-09-10, from Blake)

### Config location: `osOpts.Darwin`, not `vmOpts.vz`

AkihiroSuda's standing review convention: OS-specific guest config belongs under
`osOpts.<os>`, mirroring `WindowsOpts` / `osOpts.windows`. This is exactly why B1
had to move `suppressFirstLoginSetup` from `vmOpts.vz` to `osOpts.Darwin` during
the #5336 review. The new field lands next to it:

```yaml
osOpts:
  Darwin:
    suppressFirstLoginSetup: true        # B1 (merged) — macOS <= 26 path
    guestProvisioning:                   # this PR — macOS 27+ path
      fullName: "Lima User"
      username: lima
      password: "..."
      logsInAutomatically: true
      enablesRemoteLogin: true
```

Prefer a plain sub-struct with plain fields (the other standing convention —
no empty-struct-as-flag).

### The host/guest version matrix

| Host | Guest | Behavior |
|------|-------|----------|
| ≥ 27 | ≥ 27 | Native `VZMacGuestProvisioningOptions` on the first-boot start options |
| ≥ 27 | ≤ 26 | Fake cloud-init (B1 `suppressFirstLoginSetup` + `configure.sh`) |
| ≤ 26 | ≤ 26 | Fake cloud-init (B1) |
| ≤ 26 | ≥ 27 | **Warn** the user that the host must be upgraded to macOS 27 to use guest customization for a macOS 27 guest, then skip provisioning (do not silently half-configure) |

- **Host version** — needs a `hostMacOSMajorVersion()` helper. **None currently
  exists in the tree** (`grep` on 2026-09-10 found nothing); the old
  `hostOSMajorVersion()` left with the DFU workaround removal (2026-07-25). Both
  this PR and the guest-version PR will want it — introduce it once, shared.
- **Guest version** — comes from the **guest-OS-version-detection PR** (see
  [ipsw-build-manifest.md](ipsw-build-manifest.md)): `OperatingSystemVersion().MajorVersion`
  read from the already-loaded `VZMacOSRestoreImage` during `Create()`, persisted
  to a `vz-guest-os-version` sentinel and surfaced as `LIMA_CIDATA_GUEST_OS_VERSION`.
  That PR is a **hard prerequisite** — without the guest major version there is no
  way to implement rows 2 and 4 of the matrix.

### `configure.sh` overlap

Once the native path is stable, for the 27/27 case `configure.sh` should skip the
steps the framework now owns: password set, autologin (`logsInAutomatically`), SSH
remote login (`enablesRemoteLogin`). SSH *key* injection, chezmoi bootstrap,
screensaver/energy-saver settings, and wallpaper have no `VZMacGuestProvisioning`
equivalent and still run. Gate that skip on `LIMA_CIDATA_GUEST_OS_VERSION >= 27`
plus a marker that native provisioning actually ran.

---

## Where changes land (conceptual)

- `pkg/limatype/lima_yaml.go` — `DarwinOpts` gains `GuestProvisioning *DarwinGuestProvisioning`
  (moved from `VZOpts`); the `VZGuestProvisioning` struct is renamed/relocated.
- `pkg/driver/vz/` — the CGo/ObjC provisioning module (from the existing branch),
  plus the first-boot start-options wiring in the install/create flow; a shared
  `hostMacOSMajorVersion()` helper.
- The install path (`VZMacOSInstaller` completion → transient provisioning boot →
  stop → normal lifecycle) gains the version-matrix branch: native vs. fake
  cloud-init vs. warn-and-skip.
- `configure.sh` (in this repo, `lima_mac`) — conditional skip of the now-native
  steps for 27/27.
- Docs: `website/content/en/docs/usage/guests/macos.md`.

---

## 2026-10-05 Working end to end (lima-devl P1 r4)

`make rebuild-27-custo` (macOS 27.0.1 `26A434` guest, macOS 27 host, lima-devl
`2.3.0-dev.20261004_4`) completes. Setup Assistant creates the account from
`VZMacGuestProvisioningOptions`; Lima adopts it. Verified in the guest: one user
`blake` (uid 501, admin, home `/Users/blake`), provisioning password accepted and
matching `~/password`, `autoLoginUser=blake`, Remote Login on, Lima SSH keys and
passwordless sudo in place, both boots READY, clean desktop with no first-login
screens.

### How Apple's mechanism works (reverse-engineered, not documented)

The docs and SDK headers only say the options apply "on the first boot after
restore" via "the automated guest configuration protocol". What we found:

- Host: `-[VZMacGuestProvisioningOptions _createSAFacelessUserConfiguration]` —
  the options become a **Setup Assistant faceless user configuration**. The public
  API sits on a generic private `_setGuestProvisioningOptions:forApplicationIdentifier:`.
- Guest: the kernel publishes `AppleVirtualPlatformProvisioningMessenger` /
  `AppleVirtualPlatformProvisioningService` (PCI) on every boot;
  `AppleVirtualPlatformGuestProvisioningOptions` fetches the payload by application
  identifier. **Setup Assistant is the consumer** — if it doesn't run, the options
  are silently ignored (first test: options delivered, no effect at all).
- Setup Assistant writes `/var/db/.AppleSetupDone` itself when it finishes.

### Mismatches between Apple's model and Lima's macOS guest model

| Area | Apple (guest provisioning) | Lima today | What P1 does |
|------|----------------------------|------------|--------------|
| Setup Assistant | Must run on first boot; it applies the options | Disk patch writes `.AppleSetupDone` + `.skipbuddy` so it never runs | Removes both markers after patching when provisioning is pending (r2) |
| Who creates the user | Setup Assistant (faceless) | fakecloudinit `sysadminctl -addUser` | fakecloudinit waits for the account in directory services and adopts it; no `sysadminctl` (r3) |
| Password | Host supplies it | Guest generates a random one, writes `~/password` | Provisioning password goes to cidata as a separate `guest-provisioning-password` file (not in `user-data`); fakecloudinit writes it to `~/password` (r3) |
| Home directory | Always `/Users/<username>`; no API field to change it | `/Users/<user>.guest`, derived from the host username (avoids colliding with a host home mounted at `/Users/<user>`) | r9: Lima's normal defaults (`user.name` = host user, `user.home` = `/Users/<user>.guest`). fakecloudinit moves the home Setup Assistant created to `user.home` before the first login (see "2026-11-13 Moving the home"). r5/r6 instead named the account `<host user>.guest` so Apple's home matched; r4 overrode the home in cidata. |
| Later boots | Options never re-applied | "Already set up" = `/Users/<user>.guest` exists | Also treats an existing directory-services account as set up |
| Auto-login / Remote Login | `logsInAutomatically` / `enablesRemoteLogin` | fakecloudinit enables sshd; lima_mac `configure.sh` handles auto-login | Remote Login comes from Apple; fakecloudinit still loads sshd (harmless). Auto-login comes from Apple only when the home doesn't move; when it moves (the default), Lima turns Apple's off and writes `/etc/kcpassword` + `autoLoginUser` itself after the move, so auto-login starts from the second boot (r9) |
| Config surface | Five fields on one object | n/a | `osOpts.Darwin.guestProvisioning` keeps only `password`, `logsInAutomatically`, `enablesRemoteLogin`; username and full name come from `user.name` / `user.comment` (r5). lima_mac passes only the password (random per run, `PROVISION_PASSWORD`). |

Consequences to resolve before anything goes upstream (#5516):

- **Host-home mount collision: avoided by moving the home** (r9). The home ends up at
  `user.home` (default `/Users/<host user>.guest`), so a host home mounted at
  `/Users/<host user>` doesn't shadow it. r5/r6 avoided it by naming the account
  `<host user>.guest` instead, and rejected any `user.home` other than
  `/Users/<user.name>`; r9 drops both.
- **First boot stops at the login window** when the home moves, because Apple's
  auto-login is turned off so the old home is never logged into. Auto-login starts
  from the second boot.
- **Who owns the account fields.** Username and full name now come from the Lima user
  (r5), as #5516 suggests. The password is still configured, not generated by Lima.
- **Password at rest:** plaintext in `lima.yaml` and on the cidata ISO, same exposure
  as before.
- **Leftover r1/r2-era behaviour:** `suppressFirstLoginSetup` still runs in the
  adoption path; with Setup Assistant doing its own setup it may be unnecessary —
  untested whether turning it off changes the first-login screens.
- **configure.sh warnings (non-fatal) on this run:** user wallpaper skipped
  ("Finder not responding") and Terminal AppleEvents approval failed — privacy
  prompts for the new account, not provisioning failures.

## 2026-10-08 Adopted for the regular macOS 27 VM

The `macos-27-custo` test VM was folded into `macos-27`: `macos-27.yaml` now carries
`osOpts.Darwin.guestProvisioning`, `make build-27` creates the VM with a random initial
`PROVISION_PASSWORD` (kept in the guest's `~/password`; `configure.sh`
`configure_password` replaces it with `MACOS_PASSWORD` from `.envrc` when that is set),
and the `macOS_27_custo` runner, label and workflow input are gone. The VM is built the
same way as 26/15: Lima/Apple provision the account, then `tools/macos-setup` customizes
the disk from the host (wallpaper, screensaver, energy, TCC AppleEvents). Checked on a
clean `make rebuild-27`: account `<user>.guest` (r6 naming), passwordless sudo, Finder and Terminal
AppleEvents without a dialog, netatalk CI green on `macOS_27`. Not exercised: a
non-empty `MACOS_PASSWORD` on top of a provisioned account.

## 2026-11-13 Moving the home (lima-devl P1 r8/r9)

Blake's idea: keep Lima's normal account (`user.name` = host user) and let Setup
Assistant create `/Users/<user>`, then move the home to `user.home` and repoint the
account with `dscl`, before anyone logs in.

**How it works (r9).** On the first boot, cidata carries `guest-provisioning-password`
(and `guest-provisioning-autologin` when `logsInAutomatically` is set and the home
moves). fakecloudinit (`io.lima-vm.lima-macos-init`, as root):

1. Waits for the account in directory services, then for `/var/db/.AppleSetupDone`.
2. `launchctl bootout user/<uid>`. Setup Assistant leaves a background launchd
   session for the new user at the login window (`distnoted`, `cfprefsd`, `trustd`,
   `secd`, `lsd`, `pkd`, `containermanagerd`, `mdbulkimport`, `mbbackgrounduse`);
   moving the home under it isn't safe.
3. Waits until `pgrep -U <uid>` finds nothing.
4. `mv /Users/<user> <user.home>`, `dscl . -create /Users/<user> NFSHomeDirectory
   <user.home>`, `dscacheutil -flushcache`, then checks the account reads back the new
   home. If `dscl` fails the directory is moved back. If the move fails, it warns and
   symlinks `user.home` to Apple's home so the boot scripts still work.
5. Installs `~/password`, SSH keys and the rest as before, then writes `/etc/kcpassword`
   (XOR key `7D 89 52 23 D2 BC DD EA A3 B9 1F`, padded to a multiple of 12) and
   `defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser`.

The vz driver turns Apple's `logsInAutomatically` off whenever the home moves, so
the user is never logged in with the old home.

**r8 failures, fixed in r9.**
- The "no processes for the uid" wait timed out because of Setup Assistant's
  background session (above), so the move was skipped. r9 adds the `bootout`.
- r8's fallback kept Apple's home, and lima_mac's boot script then failed with
  `cp: /Users/blake.guest/.lima-user-script: No such file`. r9 symlinks instead.
- `killall loginwindow` after writing kcpassword does **not** log the user in on 27.2
  (the screenshot still showed the login window); the same kcpassword logged in on
  the next boot. r9 drops the `killall`.

**Verified** on far-star with `make rebuild-27` (macOS 27.2 host and guest): the home
moved about 5 s into the first boot and the VM was READY about 20 s later; the second
boot auto-logged in with no first-login screens. In the guest: uid 501 `blake`, full
name from the host, `NFSHomeDirectory` and `$HOME` `/Users/blake.guest`, no
`/Users/blake`; SecureToken enabled and `fdesetup list` shows `blake`; the login
keychain unlocks with `~/password`; no preferences point at the old path;
passwordless sudo; the `macOS_27` runner registered from `/Users/blake.guest` and
netatalk CI passed on it.

**Side effect:** on the first boot `configure.sh`'s user `defaults write` steps log
`Could not write domain` (no user session yet). Non-fatal; they run cleanly on the
second boot.

**Watching the console:** `limactl screenshot macos-27 -o out.png` works on 27.2 as
long as the limactl app isn't hidden (Cmd-H gives "no data"); unhide its window first.

## Open questions

- Does `VZMacGuestProvisioningOptions` interact with `suppressFirstLoginSetup`?
  On a 27/27 combo, do we still set `suppressFirstLoginSetup`, or does native
  provisioning imply it? Needs testing on a 27 host.
- Does the transient provisioning boot need `startUpFromMacOSRecovery`? Almost
  certainly not, but confirm.
- Validation timing: call `validateWithError:` explicitly before the boot, or rely
  on `setGuestProvisioningOptions:error:` to validate? The latter is enough.
- Password in plaintext YAML — same exposure as B1 / `configure.sh` today; note it,
  don't try to solve it here.
