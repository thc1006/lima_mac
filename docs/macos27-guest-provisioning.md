# macOS 27 Guest Provisioning (`VZMacGuestProvisioningOptions`)

Research + design notes for the planned upstream PR that enables Apple's native
macOS-guest first-boot provisioning. This is the **macOS 27+ successor** to the
fake-cloud-init work merged in **PR #5336** (`osOpts.Darwin.suppressFirstLoginSetup`,
Issue #5186). Fake cloud-init stays the path for macOS ≤ 26 guests; this feature
takes over when host **and** guest are both macOS 27+.

Companion doc: [ipsw-build-manifest.md](ipsw-build-manifest.md) — the guest-OS-version
detection this feature depends on (the "PR 1" prerequisite).

Status as of 2026-10-07: **working end to end** in lima-devl P1 r6 (account `<host user>.guest`; first working build was r4 on 2026-10-05) (local only,
forked Code-Hex/vz) — see "2026-10-05 Working end to end" below for findings and
the Apple-vs-Lima mismatches.

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
| Home directory | Always `/Users/<username>`; no API field to change it | `/Users/<user>.guest`, derived from the host username (avoids colliding with a host home mounted at `/Users/<user>`) | The **account name** is chosen so Apple's home equals Lima's usual one: with guest provisioning, `user.name` defaults to `<host user>.guest` and `user.home` to `/Users/{{.User}}` (set in `FillDefault`, r6). r4's cidata-level home override is gone. A dotted name is accepted by Apple (r5 tested `lima.<user>`, r6 `<user>.guest`). |
| Later boots | Options never re-applied | "Already set up" = `/Users/<user>.guest` exists | Also treats an existing directory-services account as set up |
| Auto-login / Remote Login | `logsInAutomatically` / `enablesRemoteLogin` | fakecloudinit enables sshd; lima_mac `configure.sh` handles auto-login | Both now come from Apple; fakecloudinit still loads sshd (harmless) |
| Config surface | Five fields on one object | n/a | `osOpts.Darwin.guestProvisioning` keeps only `password`, `logsInAutomatically`, `enablesRemoteLogin`; username and full name come from `user.name` / `user.comment` (r5). lima_mac passes only the password (random per run, or `CUSTO_PASSWORD`). |

Consequences to resolve before anything goes upstream (#5516):

- **Host-home mount collision: avoided by naming.** The account is `<host user>.guest`,
  so a host home mounted at `/Users/<host user>` no longer shadows it. (An explicit
  `user.name` equal to the host user would reintroduce the clash.)
- **Explicit `user.home` is rejected** unless it equals `/Users/<user.name>` (validation
  added in r5), since the boot scripts need the home Setup Assistant creates.
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
clean `make rebuild-27`: account `<user>.guest`, passwordless sudo, Finder and Terminal
AppleEvents without a dialog, netatalk CI green on `macOS_27`. Not exercised: a
non-empty `MACOS_PASSWORD` on top of a provisioned account.

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
