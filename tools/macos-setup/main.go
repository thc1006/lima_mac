// macos-setup customizes a stopped Lima macOS guest by editing its disk image
// from the host.
//
// It works like Lima's own guest patch (pkg/guestpatch/macos in lima): attach the
// image without mounting, mount the Data volume with `noowners` (so no sudo),
// write files, unmount, detach. Run it after Lima has finished provisioning, so the
// guest user and its home already exist; user-level settings can then be written
// directly, with no GUI session, cliclick or TCC prompt.
//
// A `noowners` mount stores every new file as uid 99. Files that macos-setup
// creates are therefore re-owned afterwards by patching the APFS inode records on
// the raw container device (chown.go, copied from lima's pkg/apfs, which does the
// same for the files Lima writes). Existing files are rewritten in place and keep
// their owner.
//
// Usage: macos-setup [-limactl limactl] [-uid 501] [-gid 20] <instance>
package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strings"
	"time"

	"howett.net/plist"
)

const (
	rootUID, rootGID = 0, 0
)

type owner struct{ uid, gid uint32 }

// volume is the mounted Data volume of the guest plus the bookkeeping needed to
// fix ownership of what was created.
type volume struct {
	mnt     string // host mount point
	instDir string // Lima instance directory
	major   int    // guest macOS major version
	user    string // guest user name
	homeDir string // guest home, relative to the volume root (Users/<name>.guest by default)
	userOwn owner  // guest user's uid/gid
	created map[string]owner
	order   []string // creation order, parents first
}

func (v *volume) abs(rel string) string { return filepath.Join(v.mnt, rel) }

func (v *volume) home(rel string) string { return path.Join(v.homeDir, rel) }

// note records that rel was created and must be re-owned to o.
func (v *volume) note(rel string, o owner) {
	if _, ok := v.created[rel]; !ok {
		v.created[rel] = o
		v.order = append(v.order, rel)
	}
}

// mkdirAll creates rel and any missing parents, remembering the new ones.
func (v *volume) mkdirAll(rel string, o owner) error {
	var missing []string
	for p := rel; p != "." && p != "/" && p != ""; p = path.Dir(p) {
		if _, err := os.Stat(v.abs(p)); err == nil {
			break
		}
		missing = append([]string{p}, missing...)
	}
	for _, p := range missing {
		if err := os.Mkdir(v.abs(p), 0o755); err != nil {
			return err
		}
		v.note(p, o)
	}
	return nil
}

// writeFile writes data to rel. An existing file is rewritten in place, which keeps
// its owner; a new file is created and queued for chown.
func (v *volume) writeFile(rel string, data []byte, mode os.FileMode, o owner) error {
	_, err := os.Stat(v.abs(rel))
	exists := err == nil
	if !exists {
		if err := v.mkdirAll(path.Dir(rel), o); err != nil {
			return err
		}
	}
	if err := os.WriteFile(v.abs(rel), data, mode); err != nil {
		return err
	}
	if !exists {
		v.note(rel, o)
	}
	return nil
}

// editPlist applies fn to the plist at rel (an empty one if it does not exist yet)
// and writes it back as a binary plist, as cfprefsd does.
func (v *volume) editPlist(rel string, mode os.FileMode, o owner, fn func(map[string]any)) error {
	dict := map[string]any{}
	if b, err := os.ReadFile(v.abs(rel)); err == nil {
		if _, err := plist.Unmarshal(b, &dict); err != nil {
			return fmt.Errorf("parse %s: %w", rel, err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	fn(dict)
	out, err := plist.Marshal(dict, plist.BinaryFormat)
	if err != nil {
		return err
	}
	return v.writeFile(rel, out, mode, o)
}

// task is one customization.
type task struct {
	name string
	run  func(*volume) error
}

var tasks = []task{
	{"init script", taskInitScript},
	{"wallpaper", taskWallpaper},
	{"screensaver and screen lock", taskScreensaver},
	{"energy saver", taskEnergy},
	{"Terminal and Finder AppleEvents (TCC)", taskTCC},
}

func main() {
	limactl := flag.String("limactl", "limactl", "limactl binary")
	uid := flag.Uint("uid", 501, "uid of the guest user (the first account Setup Assistant creates)")
	gid := flag.Uint("gid", 20, "gid of the guest user (staff)")
	flag.Parse()
	if flag.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: macos-setup [flags] <instance>")
		os.Exit(2)
	}
	if err := run(*limactl, flag.Arg(0), owner{uint32(*uid), uint32(*gid)}); err != nil {
		fmt.Fprintln(os.Stderr, "macos-setup:", err)
		os.Exit(1)
	}
}

func logf(format string, a ...any) {
	fmt.Printf("\033[0;32m[macos-setup]\033[0m "+format+"\n", a...)
}

func run(limactl, instance string, userOwn owner) error {
	out, err := exec.Command(limactl, "list", instance, "--format",
		"{{.Status}}\t{{.Dir}}\t{{.Config.User.Name}}\t{{.Config.User.Home}}").Output()
	if err != nil {
		return fmt.Errorf("no such instance %q: %w", instance, err)
	}
	f := strings.Split(strings.TrimSpace(string(out)), "\t")
	if len(f) != 4 || f[2] == "" || f[3] == "" {
		return fmt.Errorf("cannot read instance details from %q", out)
	}
	status, dir, user, home := f[0], f[1], f[2], f[3]
	if status != "Stopped" {
		return fmt.Errorf("%s is %s; stop it first (the disk image cannot be attached while the VM holds it)", instance, status)
	}
	disk := filepath.Join(dir, "disk")

	v := &volume{
		mnt:     filepath.Join(dir, "mnt-macos-setup"),
		instDir: dir,
		user:    user,
		homeDir: strings.TrimPrefix(path.Clean(home), "/"),
		userOwn: userOwn,
		created: map[string]owner{},
	}
	if v.major, err = guestMajor(dir); err != nil {
		return err
	}
	if err := writePhase(disk, v); err != nil {
		return err
	}
	return chownPhase(disk, v)
}

// guestMajor returns the guest's macOS major version from the sentinel Lima writes
// when the instance is created (vz-guest-os-version, e.g. "26.6.2").
func guestMajor(instDir string) (int, error) {
	b, err := os.ReadFile(filepath.Join(instDir, "vz-guest-os-version"))
	if err != nil {
		return 0, fmt.Errorf("guest version: %w", err)
	}
	var major int
	if _, err := fmt.Sscanf(strings.TrimSpace(string(b)), "%d", &major); err != nil {
		return 0, fmt.Errorf("parse guest version %q: %w", b, err)
	}
	return major, nil
}

type attached struct{ container, data string }

func attach(disk string) (*attached, error) {
	var out []byte
	var err error
	for range 3 { // Lima retries too: the image may still be held for a moment
		cmd := exec.Command("diskutil", "image", "attach", "-plist", "-nomount", disk)
		cmd.Env = append(os.Environ(), "LANG=C", "LC_ALL=C")
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		out, err = cmd.Output()
		if err == nil || !strings.Contains(stderr.String(), "Resource temporarily unavailable") {
			if err != nil {
				err = fmt.Errorf("%w: %s", err, stderr.String())
			}
			break
		}
		time.Sleep(3 * time.Second)
	}
	if err != nil {
		return nil, fmt.Errorf("attach %s: %w", disk, err)
	}
	var p struct {
		Entities []map[string]any `plist:"system-entities"`
	}
	if _, err := plist.Unmarshal(out, &p); err != nil {
		return nil, err
	}
	a := &attached{}
	for _, e := range p.Entities {
		dev, _ := e["dev-entry"].(string)
		if dev == "" {
			continue
		}
		if hint, _ := e["content-hint"].(string); hint == "Apple_APFS" {
			a.container = dev
		}
		if role, _ := e["role"].(string); role == "Data" {
			a.data = dev
		}
	}
	if a.data == "" {
		return nil, errors.New("no Data volume found in " + disk)
	}
	return a, nil
}

// detach detaches the data slice, which detaches the whole image (as Lima does).
func (a *attached) detach() {
	if out, err := exec.Command("hdiutil", "detach", a.data).CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "detach %s: %v: %s\n", a.data, err, out)
	}
}

func writePhase(disk string, v *volume) (err error) {
	a, err := attach(disk)
	if err != nil {
		return err
	}
	defer a.detach()

	if err := os.MkdirAll(v.mnt, 0o755); err != nil {
		return err
	}
	defer os.Remove(v.mnt)
	if out, err := exec.Command("mount", "-t", "apfs", "-o", "noowners", a.data, v.mnt).CombinedOutput(); err != nil {
		return fmt.Errorf("mount %s: %w: %s", a.data, err, out)
	}
	defer func() {
		if out, uerr := exec.Command("umount", v.mnt).CombinedOutput(); uerr != nil && err == nil {
			err = fmt.Errorf("umount %s: %w: %s", v.mnt, uerr, out)
		}
	}()

	if _, err := os.Stat(v.abs(v.home(""))); err != nil {
		return fmt.Errorf("home of %s not found; has the guest finished its first boot? (%w)", v.user, err)
	}
	logf("patching %s (user %s)", disk, v.user)
	for _, t := range tasks {
		if err := t.run(v); err != nil {
			return fmt.Errorf("%s: %w", t.name, err)
		}
		logf("%s", t.name)
	}
	return nil
}

// chownPhase fixes the owner of every file created in writePhase. It has to run on
// the raw container device with the volume unmounted.
func chownPhase(disk string, v *volume) error {
	if len(v.order) == 0 {
		return nil
	}
	a, err := attach(disk)
	if err != nil {
		return err
	}
	defer a.detach()
	if a.container == "" {
		return errors.New("diskutil did not report an APFS container device")
	}
	dev := "/dev/r" + strings.TrimPrefix(a.container, "/dev/")
	for _, rel := range v.order { // parents first
		o := v.created[rel]
		if err := Chown(dev, VolRoleData, o.uid, o.gid, rel); err != nil {
			return fmt.Errorf("chown %s: %w", rel, err)
		}
	}
	logf("re-owned %d created path(s)", len(v.order))
	return nil
}

// ── Tasks ─────────────────────────────────────────────────────────────────────

// taskInitScript makes Lima's init script wait for the cidata volume contents.
// /usr/local/sbin/lima-macos-init.sh only checks that /Volumes/cidata exists. After a
// clean unmount the guest boots fast enough for it to run while cidata is mounted
// but not yet readable ("lima-guestagent: No such file"); fake-cloud-init then never
// runs and `limactl start` waits forever on "boot scripts must have finished".
func taskInitScript(v *volume) error {
	const initSh = `#!/bin/sh
set -eux
date
# wait for cidata: the volume can be mounted before its contents are readable
i=0
while [ ! -x /Volumes/cidata/lima-guestagent ]; do
  i=$((i + 1))
  if [ "$i" -gt 120 ]; then
    echo "/Volumes/cidata/lima-guestagent did not appear" >&2
    exit 1
  fi
  sleep 1
done
exec /Volumes/cidata/lima-guestagent fake-cloud-init
`
	return v.writeFile("usr/local/sbin/lima-macos-init.sh", []byte(initSh), 0o755, owner{rootUID, rootGID})
}

const (
	wallpaperFile = "/System/Library/Desktop Pictures/Solid Colors/Space Gray.png"
)

// taskWallpaper sets the Space Gray solid colour as the desktop picture, in the
// form System Settings stores it (copied from a macOS 27 guest where it was set by
// hand): provider com.apple.wallpaper.choice.image with the picture and the colour
// options as embedded binary plists. It is applied to AllSpacesAndDisplays and
// SystemDefault, which cover every display. The login window picture is set too.
func taskWallpaper(v *volume) error {
	bplist := func(x any) []byte {
		b, err := plist.Marshal(x, plist.BinaryFormat)
		if err != nil {
			panic(err)
		}
		return b
	}
	colorSpace := bplist("kCGColorSpaceGenericRGB")
	spaceGray := []any{0.2549019607843137, 0.4117647058823529, 0.6666666666666666, 1.0}
	pictureURL := "file://" + strings.ReplaceAll(wallpaperFile, " ", "%20")

	// The stored form differs by release (both copied from guests where the colour
	// was picked by hand). macOS 26 and 27 keep the picture in Configuration and the
	// colour in EncodedOptionValues; macOS 15 and earlier keep the colour and
	// placement in Configuration and the picture in Files.
	var choice func() map[string]any
	var options any = "$null"
	if v.major >= 26 {
		choice = func() map[string]any {
			return map[string]any{
				"Configuration": bplist(map[string]any{
					"type": "imageFile",
					"url":  map[string]any{"relative": pictureURL},
				}),
				"Files":    []any{},
				"Provider": "com.apple.wallpaper.choice.image",
			}
		}
		options = bplist(map[string]any{
			"values": map[string]any{
				"color": map[string]any{"color": map[string]any{"_0": map[string]any{"color": map[string]any{
					"components": spaceGray,
					"colorSpace": colorSpace,
				}}}},
				"placement": map[string]any{"picker": map[string]any{"_0": map[string]any{"id": "Crop"}}},
			},
		})
	} else {
		choice = func() map[string]any {
			return map[string]any{
				"Configuration": bplist(map[string]any{
					"backgroundColor": map[string]any{"components": spaceGray, "colorSpace": colorSpace},
					"placement":       1,
				}),
				"Files":    []any{map[string]any{"relative": pictureURL}},
				"Provider": "com.apple.wallpaper.choice.image",
			}
		}
	}
	now := time.Now().UTC()
	err := v.editPlist(v.home("Library/Application Support/com.apple.wallpaper/Store/Index.plist"), 0o644, v.userOwn,
		func(d map[string]any) {
			for _, key := range []string{"AllSpacesAndDisplays", "SystemDefault"} {
				entry, _ := d[key].(map[string]any)
				if entry == nil {
					entry = map[string]any{}
				}
				entry["Type"] = "linked"
				entry["Linked"] = map[string]any{
					"Content": map[string]any{
						"Choices": []any{choice()},
						"Shuffle": "$null",
					},
					"LastSet": now,
					"LastUse": now,
				}
				if v.major >= 26 {
					entry["Linked"].(map[string]any)["Content"].(map[string]any)["EncodedOptionValues"] = options
				}
				delete(entry, "Idle")
				delete(entry, "Desktop")
				d[key] = entry
			}
		})
	if err != nil {
		return err
	}
	return v.editPlist("Library/Preferences/com.apple.loginwindow.plist", 0o644, owner{rootUID, rootGID},
		func(d map[string]any) { d["DesktopPicture"] = wallpaperFile })
}

// hardwareUUID returns the guest's platform UUID, which names its per-host
// (ByHost) preference files, taken from the files the guest already has.
func (v *volume) hardwareUUID() (string, error) {
	files, _ := filepath.Glob(v.abs(v.home("Library/Preferences/ByHost/*.plist")))
	for _, f := range files {
		base := strings.TrimSuffix(filepath.Base(f), ".plist")
		if i := strings.LastIndex(base, "."); i >= 0 && len(base)-i-1 == 36 {
			return base[i+1:], nil
		}
	}
	return "", errors.New("no ByHost preferences found to take the hardware UUID from")
}

// taskScreensaver turns off the screensaver, the lock on wake and App Nap, which
// are wrong for an unattended CI VM. These are what `defaults write` would set
// (configure.sh configure_screensaver), written straight into the plists.
func taskScreensaver(v *volume) error {
	uuid, err := v.hardwareUUID()
	if err != nil {
		return err
	}
	saver := func(d map[string]any) {
		d["idleTime"] = 0
		d["askForPassword"] = 0
		d["askForPasswordDelay"] = 0
	}
	if err := v.editPlist(v.home("Library/Preferences/com.apple.screensaver.plist"), 0o600, v.userOwn, saver); err != nil {
		return err
	}
	if err := v.editPlist(v.home("Library/Preferences/ByHost/com.apple.screensaver."+uuid+".plist"), 0o600, v.userOwn, saver); err != nil {
		return err
	}
	if err := v.editPlist("Library/Preferences/com.apple.screensaver.plist", 0o644, owner{rootUID, rootGID},
		func(d map[string]any) { d["loginWindowIdleTime"] = 0 }); err != nil {
		return err
	}
	return v.editPlist(v.home("Library/Preferences/.GlobalPreferences.plist"), 0o600, v.userOwn,
		func(d map[string]any) { d["NSAppSleepDisabled"] = true })
}

// taskEnergy is `pmset -a sleep 0 displaysleep 0 disksleep 0 standby 0 powernap 0
// womp 0` (configure.sh configure_screensaver) as the plists powerd reads at boot:
// com.apple.PowerManagement.plist and its per-host sibling. The key names and
// values are what pmset wrote on a booted guest.
func taskEnergy(v *volume) error {
	uuid, err := v.hardwareUUID()
	if err != nil {
		return err
	}
	root := owner{rootUID, rootGID}
	if err := v.editPlist("Library/Preferences/com.apple.PowerManagement.plist", 0o644, root,
		func(d map[string]any) {
			ac, _ := d["AC Power"].(map[string]any)
			if ac == nil {
				ac = map[string]any{}
			}
			ac["System Sleep Timer"] = 0
			ac["Display Sleep Timer"] = 0
			ac["Disk Sleep Timer"] = 0
			ac["Wake On LAN"] = 0
			ac["DarkWakeBackgroundTasks"] = 0 // powernap
			d["AC Power"] = ac
		}); err != nil {
		return err
	}
	return v.editPlist("Library/Preferences/com.apple.PowerManagement."+uuid+".plist", 0o644, root,
		func(d map[string]any) {
			ac, _ := d["AC Power"].(map[string]any)
			if ac == nil {
				ac = map[string]any{}
			}
			ac["Standby Enabled"] = 0
			ac["Standby Delay"] = 0
			d["AC Power"] = ac
		})
}

// tccColumns are the access columns taskTCC writes. They have been stable across the
// databases seen so far (macOS 26.6.2: admin version 32; 27.0.1: version 36, which
// only appended columns), so they, not the version number, decide whether an
// existing database can be written safely.
var tccColumns = []string{
	"service", "client", "client_type", "auth_value", "auth_reason", "auth_version", "csreq",
	"policy_id", "indirect_object_identifier_type", "indirect_object_identifier",
	"indirect_object_code_identity", "flags", "last_modified",
}

// tccAdminVersion reads the admin version row of a TCC database.
func tccAdminVersion(db string) (string, error) {
	out, err := exec.Command("sqlite3", db, "SELECT value FROM admin WHERE key='version'").Output()
	if err != nil {
		return "", fmt.Errorf("read TCC admin version of %s: %w", db, err)
	}
	return strings.TrimSpace(string(out)), nil
}

// createUserTCC creates the user's TCC database with the layout of the guest's own
// system database (same schema and admin version), so it matches whichever macOS
// release is installed without a schema table kept here.
func createUserTCC(sysDB, db string) error {
	schema, err := exec.Command("sqlite3", sysDB, ".schema").Output()
	if err != nil {
		return fmt.Errorf("read schema of %s: %w", sysDB, err)
	}
	return sqlite(db, string(schema)+fmt.Sprintf(
		"ATTACH '%s' AS sys;INSERT INTO admin SELECT * FROM sys.admin;INSERT INTO integrity_flag SELECT * FROM sys.integrity_flag;",
		strings.ReplaceAll(sysDB, "'", "''")))
}

// checkTCC verifies that a database can take our rows: every column we write must
// exist (error otherwise), and its admin version is compared with the system
// database's; a difference is only reported, since version bumps have so far been
// additive.
func checkTCC(db, sysDB string) error {
	out, err := exec.Command("sqlite3", db, "SELECT name FROM pragma_table_info('access')").Output()
	if err != nil {
		return fmt.Errorf("read access columns of %s: %w", db, err)
	}
	have := map[string]bool{}
	for _, c := range strings.Fields(string(out)) {
		have[c] = true
	}
	var missing []string
	for _, c := range tccColumns {
		if !have[c] {
			missing = append(missing, c)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("TCC database has an unknown layout (missing columns %v); update tccColumns from a booted guest", missing)
	}
	got, err := tccAdminVersion(db)
	if err != nil {
		return err
	}
	want, err := tccAdminVersion(sysDB)
	if err != nil {
		return err
	}
	if got != want {
		fmt.Fprintf(os.Stderr, "macos-setup: warning: user TCC admin version is %s, system is %s; columns match so continuing\n", got, want)
	}
	return nil
}

// taskTCC pre-approves the AppleEvents access that ssh sessions need to drive
// Finder and Terminal (osascript from a runner job). The two rows are what tccd wrote
// when the consent dialog was approved by hand on a macOS 26 guest: client
// /usr/libexec/sshd-keygen-wrapper (a path, client_type 1), auth_value 2 (allowed),
// auth_reason 3 (user consent), and the code requirements (csreq) of the client
// and of the target app as blobs. The user database is created, from the system
// database's layout, if the guest has not made one yet.
func taskTCC(v *volume) error {
	const rel = "Library/Application Support/com.apple.TCC/TCC.db"
	const sysRel = rel
	db := v.home(rel)
	// Work on a copy of the system database: sqlite3 opening the guest's own file
	// could leave journal files behind on the guest volume.
	sysCopy, err := os.ReadFile(v.abs(sysRel))
	if err != nil {
		return fmt.Errorf("system TCC database: %w", err)
	}
	tmp, err := os.CreateTemp("", "tcc-system-*.db")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(sysCopy); err != nil {
		return err
	}
	tmp.Close()
	sysDB := tmp.Name()
	if _, err := os.Stat(v.abs(db)); errors.Is(err, os.ErrNotExist) {
		if err := v.mkdirAll(path.Dir(db), v.userOwn); err != nil {
			return err
		}
		if err := createUserTCC(sysDB, v.abs(db)); err != nil {
			return err
		}
		v.note(db, v.userOwn)
	}
	if err := checkTCC(v.abs(db), sysDB); err != nil {
		return err
	}
	const sshdCsreq = "FADE0C000000003C0000000100000006000000020000001D636F6D2E6170706C652E737368642D6B657967656E2D7772617070657200000000000003"
	targets := []struct{ id, identity string }{
		{"com.apple.finder", "FADE0C000000002C00000001000000060000000200000010636F6D2E6170706C652E66696E64657200000003"},
		{"com.apple.Terminal", "FADE0C000000003000000001000000060000000200000012636F6D2E6170706C652E5465726D696E616C000000000003"},
	}
	var sql strings.Builder
	for _, t := range targets {
		fmt.Fprintf(&sql, "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, csreq, "+
			"policy_id, indirect_object_identifier_type, indirect_object_identifier, indirect_object_code_identity, flags, last_modified) "+
			"VALUES ('kTCCServiceAppleEvents', '/usr/libexec/sshd-keygen-wrapper', 1, 2, 3, 1, X'%s', NULL, 0, '%s', X'%s', NULL, CAST(strftime('%%s','now') AS INTEGER));",
			sshdCsreq, t.id, t.identity)
	}
	return sqlite(v.abs(db), sql.String())
}

func sqlite(db, sql string) error {
	cmd := exec.Command("sqlite3", db, sql)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("sqlite3 %s: %w: %s", db, err, out)
	}
	return nil
}
