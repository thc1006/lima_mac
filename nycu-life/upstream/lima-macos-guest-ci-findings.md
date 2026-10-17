# Lima macOS-guest findings (candidate upstream contributions)

Field notes from standing up a **headless, unattended macOS CI runner** on a Mac mini
(Apple M4 / 16 GB, macOS 26.5 Tahoe host, Lima **v2.2.0**, `vz` driver, macOS 26.5 guest).
Each item is written so it can be filed upstream (lima-vm/lima docs or issues). Nothing here
is a secret; versions/builds are pinned for reproducibility.

> Scope note (so we don't re-report fixed things): the macOS-26 GUI `SIGTRAP` crash
> (lima-vm/lima#4743) is **already fixed** by PR #5036 and is present in v2.2.0 — confirmed,
> our macOS guest reaches the HostKey/boot stage, never SIGTRAPs. Not a current defect.

---

## 1. macOS guest fails to boot over headless SSH — login keychain must be UNLOCKED  ★ most useful

**Symptom.** `limactl start <macos-guest>` from a non-GUI/SSH session fails at VM start:

```
failed to install macOS: Error Domain=VZErrorDomain Code=10007
 ... NSUnderlyingError=Error Domain=VZErrorDomain Code=-9
     "The virtual machine encountered a security error."
     ... "Failed to get current host key." / "Failed to create new HostKey."
     ... NSPOSIXErrorDomain Code=22 "Invalid argument"
```

**Root cause.** Virtualization.framework needs the host's **login keychain unlocked** to create
the macOS VM's HostKey / security domain. A headless or SSH-launched session leaves
`login.keychain-db` locked, so HostKey creation fails. **Linux guests are unaffected** (they
have no HostKey), which is the tell: on the same host, Linux guests boot and only macOS guests
trip this. (Same family as the tart FAQ / openai/tart#1146.)

**Workaround (works fully headless).** Unlock the keychain in the same context before start:

```sh
security unlock-keychain -p "$MAC_PASSWORD" ~/Library/Keychains/login.keychain-db
limactl start <macos-guest>
```

An interactive GUI login (or auto-login) also unlocks it, and that unlock is then visible to an
SSH-launched `limactl`.

**Suggested upstream action.** For `vmType: vz` + `os: Darwin`, detect a locked login keychain
and emit an actionable error ("login keychain is locked; run `security unlock-keychain` or use a
GUI/auto-login session"), and add a note to the macOS-guest docs. The raw `Code=-9 /
Failed to create new HostKey` is very hard to diagnose.

---

## 2. `host < guest` macOS version surfaces as the SAME `Code=-9`, indistinguishable from #1

**Symptom.** With the keychain unlocked, a guest whose build is **newer** than the host still
fails with the identical `Code=-9 / Failed to create new HostKey`.

**Context.** The bundled `template:macos-26` currently installs macOS **26.5.2 (25F84)**. On a
host still on **26.5 (25F71)** that guest is newer → fails. Pinning the guest IPSW to the host's
build (or ≤ host) fixes it. The docs state "host macOS must be same or newer than the guest," but
the **error gives no hint** that a version mismatch is the cause (it looks exactly like #1).

**Suggested upstream action.** Before boot, compare host vs guest build and fail early with a
clear message; and/or let the macOS template resolve an IPSW ≤ the host build instead of a fixed
newer one (a moving `:latest`-style alias also breaks reproducibility — see #4).

---

## 3. `sysadminctl -autologin set` returns `error:22` on macOS 26 (breaks unattended auto-login)

Enabling auto-login for an unattended CI host via `sysadminctl -autologin set -userName U
-password P` logs `SACSetAutoLoginPassword error:22` and does nothing on macOS 26; FileVault is
off. Writing the obfuscated `/etc/kcpassword` directly + `defaults write
/Library/Preferences/com.apple.loginwindow autoLoginUser U` works (and `sysadminctl -autologin
status` then reports it ON). Worth a tools/docs note for anyone scripting a macOS CI node.

---

## 4. `limactl clone` of a macOS guest is APFS copy-on-write (instant, ~0 extra disk) ★ good news

Cloning a ~39 GB stopped macOS guest: `clone` wall time **0 s**, host free space **unchanged**
(APFS `clonefile`; `du` double-counts the shared blocks but `df` confirms no real consumption).
The clone boots, its forwarded SSH port is auto-reassigned (no collision), and the VM identity is
usable. This makes **per-job ephemeral clones** practical for macOS CI even on a 16 GB host.
Suggest documenting it as a supported pattern on the clone/macOS pages (people assume a full copy
and rule it out).

---

## 5. Minor

- The guest boots to loginwindow with **no user session and no `login.keychain-db`**; tooling that
  needs a keychain (e.g. `xcodes` storing an Apple ID session) must create+unlock one first.
- `AppleVirtIOFS "My Shared Files"` appears even when the template defines no `mounts:` — it is
  **empty** in that case (good: no host-home exposure), but the mount entry can alarm a reviewer; a
  docs note ("empty unless you declare mounts") would help.

---

*Reproduced on: MacBook-less Mac mini M4, macOS 26.5 (25F71), Lima v2.2.0, guest macOS 26.5
(25F71), Xcode 26.6 (17F113). 2026-10-18.*
