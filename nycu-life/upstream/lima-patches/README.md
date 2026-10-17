# Two fixes for lima-vm/lima

Two independent bugs in lima-vm/lima, each fixed with a regression test. Opened
upstream as **PR #5641** (plist XML escaping) and **PR #5642** (boot-session
marker); the `autostart disable` gaps are **issue #5643**. The `git am`-ready
patches below are kept for reference and re-use.

Both were checked against lima-vm/lima at `f47461ed` on macOS (Go 1.25.3): each
test fails on the current tree and passes with the patch, `go build ./...` and
`go vet` stay clean, and the existing package tests still pass. Both bugs are in
the released v2.2.1 too — `writeBootSessionMarker` is the same `os.WriteFile`,
and the daemon template interpolates `WorkDir` raw.

The patches are authored under your name with a DCO sign-off, so `git am` applies
and commits them in one step:

```sh
cd /path/to/lima
git switch -c fix/autostart-plist-xml
git am /path/to/0001-fix-autostart-escape-dynamic-values-in-launchd-plist.patch
go test ./pkg/textutil/ ./pkg/autostart/... ./pkg/store/
# push to your fork and open the PR with the text below
```

Then the same for `0002-...` on its own branch.

## 1 — launchd plists: escape every value (PR #5641)

`0001-fix-autostart-escape-dynamic-values-in-launchd-plist.patch`. PR description:

> The launchd plist templates escape only `PATH`. Every other value — `Instance`, `LimaHome`, `Binary`, `WorkDir`, and the daemon's `UserName` — goes into the XML raw. Put a `&` or `<` in one of those paths (a home under `/Users/me/R&D`, say) and the rendered plist is no longer well-formed: `plutil -lint` rejects it with "unknown ampersand-escape sequence", and launchd won't load it.
>
> This adds an `xml` function to `textutil.TemplateFuncMap` and uses it for every value in both templates. It escapes with `encoding/xml` and rejects invalid UTF-8 up front, rather than letting a stray byte become U+FFFD and change a path. `ExecuteTemplate` now registers the FuncMap, so it reaches both renderers of these plists: `renderDaemonPlist` for the boot daemon and the autostart manager's `renderTemplate` for the login agent.
>
> The test renders both templates with values full of XML metacharacters, parses the output back, and checks every value is unchanged. It fails on the current templates and passes with this change.
>
> Checked on macOS, Go 1.25.3: `go test ./pkg/textutil/ ./pkg/autostart/... ./pkg/store/` passes under `-race`; `go build ./...` and `go vet` are clean; `plutil -lint` rejects the current output and accepts the escaped output.

## 2 — boot-session marker: write it atomically (PR #5642)

`0002-fix-store-write-the-boot-session-marker-atomically.patch`. PR description:

> The PID files are written atomically — temp file, then rename — but the boot-session marker beside them is not. `writeBootSessionMarker` uses `os.WriteFile`, which truncates the file before writing it again. If `staleBootSession` reads the marker in that window it gets an empty string, decides it doesn't match the current boot ID, and returns true. `WritePIDFile` then calls `removePIDFiles`, so a PID file written during this boot — a running instance's — gets deleted.
>
> This writes the marker to a uniquely named temporary file and renames it into place, so a reader always sees a complete marker, old or new. The name is unique rather than a fixed `.tmp`, so two writers don't collide on it.
>
> The test runs four writers and four readers against one marker with a constant boot ID; since the ID never changes, `staleBootSession` must never report a previous boot. On the current code it does so tens of thousands of times a second; with this change, never — including under `-race`.
>
> Checked on macOS, Go 1.25.3: `go test ./pkg/store/` passes under `-race`; `go build ./...` and `go vet` are clean. The marker code is unchanged since e529ce08 and is in v2.2.1. The test shows the bad read within one process; it doesn't drive the cross-process PID deletion on a live host.

## Verified, left for later

Reproduced in the same tree, not patched here — each needs a design decision or
hardware this set doesn't cover.

- **`autostart disable` can leave the boot LaunchDaemon behind — filed as issue #5643.** In
  `cmd/limactl/autostart_darwin.go`, `autostartDisableAction` returns right after
  removing a LaunchAgent, so an instance that has both a login agent and a boot
  daemon keeps the daemon. It also needs `store.Inspect` to succeed, so a deleted
  instance can't be disabled; it ignores non-`ENOENT` `os.Stat` errors; and
  `daemonUninstall` removes the plist even when `launchctl bootout`/`disable`
  failed. The system label is only the instance name, so the same name under two
  users collides. A fix has to settle loaded-service identity (domain, label, UID,
  `LIMA_HOME`) and the error semantics first — better raised as an issue with these
  reproductions than sent as a quick patch.
- **VZ stop has no overall deadline.** The 30s wait starts only after the SSH
  shutdown request returns, so it doesn't bound the whole `Stop`. Overlaps #4922
  and #5619; needs native macOS stop tests.
- **CI keychain / unattended signing** belongs in the runner's signing session,
  not Lima, and needs a real Mac with a signing identity to check.
