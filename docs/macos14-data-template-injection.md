# macOS 14 Guest: Pre-Install Injection via the Data Template

**Status**: PARKED (2026-10-02). Injection point identified and a modified IPSW built, but the
install test was inconclusive — the **stock** 14.6.1 IPSW fails the same way under UTM on this
host, so the failure is not attributable to the injection. Pursuing a different workaround.

Context: [lima-vm/lima#4665](https://github.com/lima-vm/lima/issues/4665) — `limactl start` of a
macOS 14 guest fails in `guestpatch/macos.Patch` with
`mount_apfs: volume could not be mounted: Illegal byte sequence`; `diskutil` reports the Data volume
as "encrypted and locked" with "No cryptographic users". Goal here was a way to get Lima's
first-boot files onto the Data volume **before** install, so no host-side mount is needed.

---

## Findings

### 1. Why the Data volume is locked (restore ramdisk, 14.6.1)

Inspected `restored_external` on the erase-install restore ramdisk (`090-91703-094.dmg`, IMG4
payload is a plain APFS container):

- Reads a `DataEncryptionType` setting, default `Device Default`; unrecognized values fall back to
  the default.
- Logs `creating class d key for %s` and formats the Data volume `encrypted` via `newfs_apfs`.
- A class D (device) key is held by the guest's own (virtual) SEP keybag — no user, no recovery key.
  This matches #4665's "No cryptographic users": nothing exists on the host to unlock with.

### 2. Same logic in 26.6.2 — creation logic is not the 14-vs-26 difference

The 26.6.2 ramdisk (`094-96989-093.dmg`, AEA-wrapped; extracted with `ipsw extract --dmg rdisk
--ident Erase`) contains identical `DataEncryptionType` / `Device Default` / class D strings. Diffing
encryption- and volume-related strings between the two builds shows only unrelated refactors
(`LPAPFS*` → `LPStaticAPFS*`, AEA handling, SEP slot commit). Why Lima can mount a 26 guest's Data
volume but not a 14 guest's remains **unexplained** (strings-level comparison only).

### 3. The Data volume is populated from a template on the system image

`restored_external` copies `System/Library/Templates/Data` from the freshly restored System volume
onto the new Data volume (`SMUDataTemplateCopier`; log line
`copied data template from %s -> %s: … cloned items …`). Paths matching
`System/Library/Templates/Data.symlink-patterns` are symlinked instead of copied — none of Lima's
paths match.

In the 14.6.1 root filesystem image (`090-91706-091.dmg`, UDIF lzfse/ULFO, 8.4 GB; template 634 MB):

| Lima path | In template |
|---|---|
| `Library/LaunchDaemons/` | present (2 stock plists, root-owned) |
| `Library/User Template/` | present |
| `private/var/db/` | present |
| `usr/local/` | present |
| `usr/local/sbin/` | absent — must be created |
| `private/var/db/.AppleSetupDone` | absent — must be created |

Template files carry real ownership (uid 0) when mounted with `-owners on`. If the copier preserves
ownership (likely — it reports "cloned items", untested), files placed as root:wheel arrive that
way, which would make Lima's raw APFS inode `chown` step unnecessary.

### 4. Integrity checks on the root filesystem image

From `BuildManifest.plist` (erase-install VM identity):

| Entry | Path | Personalize |
|---|---|---|
| `OS` | `090-91706-091.dmg` | **False** — `Digest` is plain SHA-384 of the whole file |
| `SystemVolume` | `Firmware/090-91706-091.dmg.root_hash` | True (TSS-signed per device) |
| `Ap,SystemVolumeCanonicalMetadata` | `Firmware/090-91706-091.dmg.mtree` | True |
| `StaticTrustCache` | `Firmware/090-91706-091.dmg.trustcache` | True |

- The image on disk is **not** sealed (`Sealed: No`); the restore seals it
  (`SealSystemVolumeDuringRestore`, `SealedSystemVolumeAuthenticate`, `root_hash.img4`).
- The `OS` digest lives in the unsigned manifest, so it can be updated. The signed root hash and
  mtree cannot — an edited image is **expected** (not yet observed) to fail at seal/authenticate or
  boot.
- The IPSW stores the image uncompressed in the zip.

---

## Experiment

Built `ipsw/UniversalMac_14.6.1_23G93_Restore-lima-inject.ipsw` (git-ignored):

1. `hdiutil convert` the OS image to UDRW; attach with `-owners on`.
2. Into `System/Library/Templates/Data`, as root:wheel, wrote Lima's exact `guestpatch/macos`
   payload: `private/var/db/.AppleSetupDone`, `Library/User Template/.skipbuddy`,
   `usr/local/sbin/lima-macos-init.sh` (0755),
   `Library/LaunchDaemons/io.lima-vm.lima-macos-init.plist` (0644), plus a marker
   `private/var/db/lima-template-inject-test`.
3. Convert back to ULFO; `asr imagescan` succeeded.
4. Set `Digest` = SHA-384 of the new image on all 105 manifest entries pointing at
   `090-91706-091.dmg` (every Mac model shares it); replace both files in a clone of the IPSW
   (`zip -0`). Verified: all 1327 entry CRCs OK; manifest digest matches archived image.

### Result — inconclusive

Installed via UTM (Virtualization.framework) on a macOS 27.0.1 (26A434) host:

- Modified IPSW (09:25) and **stock** IPSW (09:35) fail identically: UTM creates a 64 GiB ASIF disk,
  `com.apple.Virtualization.Installation` starts, `com.apple.Virtualization.VirtualMachine` logs
  breadcrumb `0x7c5e66f70000007d`, and the install stops ~15 s later — far too early to have restored
  or sealed the system image.
- Installer details are redacted (`<private>`) in the unified log; UTM's on-screen error text was
  not captured.
- Signing is not a confirmed cause: ipsw.me reports 14.6.1 (23G93) still signed for `Mac14,2`; its
  `VirtualMac2,1` column shows every build unsigned, including 26/27 builds that install fine, so
  that column is not meaningful for VMs. `ipsw download tss` cannot look up macOS builds.

So the injection approach is **untested, not disproven**.

---

## Open questions

- Why does a stock 14.6.1 install fail under UTM on a 27.0.1 host? Capture UTM's error text
  (`VZErrorDomain` code), and retry via Lima (`make build-14`) for comparison.
- Once a stock 14 install works on this host: does the modified IPSW get past sealing, and does the
  marker appear on the Data volume?
- Why is a 26 guest's Data volume host-mountable when the restore creation logic is identical?
- Does the template copier preserve ownership?

## Tools

- `ipsw` (MacPorts `ipsw` 3.1.713): `ipsw extract --dmg {rdisk,fs,sys} [--ident Erase]` handles IMG4
  and AEA. Writes config to `~/.config/ipsw` (ignores `XDG_CONFIG_HOME`), so it fails in the Claude
  Code sandbox.
- `hdiutil attach` of extracted images requires running outside the Claude Code sandbox
  (DiskArbitration).
