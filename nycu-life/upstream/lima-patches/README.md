# Two fixes for lima-vm/lima (launchd plist escaping, boot-session marker)

Two self-contained bug fixes, each with a regression test. Verified against
lima-vm/lima at commit `f47461ed` on macOS (darwin/arm64, Go 1.25.3): each test
fails on the current code and passes with the patch, `go build ./...` and
`go vet` are clean, and the existing package tests still pass. Both bugs are
also present in the released **v2.2.1** (`writeBootSessionMarker` is the same
`os.WriteFile`, and the daemon template interpolates `WorkDir` raw).

Send them as **two separate PRs**. Apply a patch, read it, then commit under
your own identity with a sign-off (`git commit -s`) as Lima's CONTRIBUTING
requires. On a Mac it is worth generating a plist and running `plutil -lint`
on it before opening the first PR.

```sh
cd /path/to/your/lima/checkout
git switch -c fix/autostart-plist-xml
git apply /path/to/0001-plist-xml.patch
go test ./pkg/textutil/ ./pkg/autostart/... ./pkg/store/
git add -A && git commit -s      # write the message below
```

---

## 1. `0001-plist-xml.patch` — escape all dynamic values in launchd plists

Commit message:

```
fix(autostart): escape all dynamic values in launchd plists

Only PATH was escaped (with html); Instance, LimaHome, Binary, WorkDir and
UserName were interpolated into the plist XML raw. A path containing & or <
produces an invalid plist, and a literal &amp; decodes to a different path.

Route every dynamic value through a new xml function (it rejects invalid
UTF-8 and uses encoding/xml.EscapeText) and register TemplateFuncMap in
ExecuteTemplate so both the LaunchAgent and LaunchDaemon render paths are
covered. Plain-template and KeepAlive behaviour is unchanged.
```

PR description:

> The generated launchd plists interpolate most of their dynamic values into XML without escaping. In `pkg/autostart/launchd/io.lima-vm.autostart.INSTANCE.plist` and `io.lima-vm.daemon.INSTANCE.plist`, only `PATH` is escaped (with `html`); `Instance`, `LimaHome`, `Binary`, `WorkDir` and the daemon's `UserName` are written raw. A path that contains `&` or `<` produces an invalid plist, and a path written as a literal `&amp;` decodes back to `&`, i.e. a different path.
>
> This routes every dynamic value in both templates through a new `xml` function in `textutil.TemplateFuncMap`. The function rejects invalid UTF-8 — otherwise `encoding/xml` would substitute U+FFFD and change the value — and then uses `xml.EscapeText`. `ExecuteTemplate` now parses with `TemplateFuncMap` registered, so both render paths that already call it, the CLI's `renderDaemonPlist` and the autostart manager's `renderTemplate`, are covered. Plain-template and `KeepAlive` behaviour is unchanged.
>
> The added test renders both templates with values containing `&`, `<`, `>`, `"` and `'`, decodes the output as XML, and checks each value is unchanged. It fails on the current templates, where the rendered plist is not well-formed, and passes with this change.
>
> Verified on macOS (darwin/arm64, Go 1.25.3): `go test ./pkg/textutil/ ./pkg/autostart/... ./pkg/store/` pass under `-race`, and `go build ./...` and `go vet` are clean. `plutil`/`launchctl` were not run against a generated file; the escaping is platform-independent and the plist structure is otherwise unchanged.

---

## 2. `0002-bootsession-marker.patch` — write the boot session marker atomically

Commit message:

```
fix(store): write the boot session marker atomically

writeBootSessionMarker used os.WriteFile, which truncates the existing marker
before writing. A concurrent staleBootSession could read the empty file,
decide it does not match the current boot ID, and return true, at which point
WritePIDFile removes PID files written during the current boot.

Write to a uniquely named temporary file in the same directory and rename it
into place, so a reader sees either the old marker or the new one.
```

PR description:

> `writeBootSessionMarker` writes the marker with `os.WriteFile`, which truncates the existing file before writing the new contents. The PID file itself is written atomically, via a temporary file and a rename (`writePIDFileAtomically`), but the marker is not. A `staleBootSession` call running concurrently can read the marker inside that truncation window, get an empty string, find it does not equal the current boot ID, and return true — at which point `WritePIDFile` calls `removePIDFiles` and removes PID files written during the current boot.
>
> This writes the marker to a uniquely named temporary file in the same directory and renames it into place, so a reader sees either the previous marker or the new one, never a truncated one. The temporary name is unique (not a fixed `.tmp`) so two concurrent writers do not collide on it.
>
> The added test runs four writers and four readers against one marker with a constant boot ID; because the ID never changes, `staleBootSession` must never report a previous boot. On the current code it does so tens of thousands of times per second; with this change it never does, including under `-race`.
>
> Verified on macOS (darwin/arm64, Go 1.25.3): `go test ./pkg/store/` passes under `-race`, and `go build ./...` and `go vet` are clean. The code is unchanged since it was introduced in e529ce08 and is present in v2.2.1. The test reproduces the false result within one process; it does not reproduce PID removal across separate processes on a running host.

---

## Verified but not fixed here

Reproduced against the same source, left out on purpose: each needs a design
decision or hardware this change set does not cover.

- **`autostart disable` can leave the boot LaunchDaemon behind.** In
  `cmd/limactl/autostart_darwin.go`, `autostartDisableAction` returns right
  after removing a LaunchAgent (so an instance with both a login agent and a
  boot daemon keeps the daemon); it requires `store.Inspect` to succeed, so a
  deleted instance cannot be disabled; it ignores non-`ENOENT` `os.Stat`
  errors; and `daemonUninstall` removes the plist even when `launchctl
  bootout`/`disable` returned an error. The system label is only the instance
  name (`io.lima-vm.daemon.<name>`), so the same instance name under two users
  collides. A fix needs to settle loaded-service identity (domain, label, UID,
  `LIMA_HOME`) and the error semantics first, so it is better raised as an
  issue with these reproductions than sent as a quick patch.
- **VZ stop has no overall deadline.** The 30s wait in the vz driver starts
  only after the SSH shutdown request returns, so it does not bound the whole
  `Stop`. This overlaps open work (#4922, #5619) and needs native macOS stop
  tests.
- **CI keychain / unattended signing** belongs in the runner's own signing
  session, not in Lima, and needs a real Mac with a signing identity to verify.
