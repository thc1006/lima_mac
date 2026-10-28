#!/bin/bash
# Lima macOS 26 VM Configuration Script
#
# This script configures the macOS 26 guest VM with essential tools and settings.
# It is designed to be idempotent — safe to run multiple times.
#
# Environment Variables:
#   MACOS_PASSWORD      - Set password for the current user (optional, skipped if unset)
#   SKIP_CHEZMOI        - Skip chezmoi dotfiles provisioning (set to 1 to skip)
#   SSH_PUBLIC_KEY      - Literal public key content to write as the sole authorized_keys entry.
#                         Overrides Lima's default multi-key injection. Leave unset to keep
#                         Lima's default behavior (all ~/.ssh/*.pub keys from the host).
#   AUTO_LOGIN         - Enable automatic login for current user so the GUI session
#                         starts on boot and LaunchAgent runner services auto-start.
#                         Defaults to 1 (on). Set to 0 to show the login screen.
#                         WARNING: bypasses the login screen — only use in trusted envs.
#   GITHUB_TOKEN       - Personal access token with repo/actions scope for runner setup.
#                         Required for configure_runner; skipped if unset.
#   RUNNER_LABEL       - GitHub Actions runner name (e.g. macOS_26). Required for runner setup.
#   GITHUB_OWNER       - GitHub owner for runner registration (default: trodemaster).
#   GITHUB_REPO        - GitHub repo for runner registration (default: blakeports).
#
# Usage:
#   ./configure.sh                                # Run all configuration steps
#   MACOS_PASSWORD="mypass" ./configure.sh        # Set password during config
#   SKIP_CHEZMOI=1 ./configure.sh                 # Skip chezmoi provisioning
#   MACOS_PASSWORD="mypass" SKIP_CHEZMOI=1 ./configure.sh  # Password only, no dotfiles

set -eux -o pipefail

# ── Environment Setup ──────────────────────────────────────────────────────────

# Load environment variables from .envrc if it exists
# This allows passing secrets and custom configuration to the script
if [[ -f /Volumes/lima_mac/.envrc ]]; then
    set +ex
    source /Volumes/lima_mac/.envrc
    set -ex
fi

# Color output for readability
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $@"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $@"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $@"
}

# Run a command (or function) with a wall-clock cap, killing it if it hangs.
# Needed because `defaults write`/`pmset`/`plutil` round-trip through cfprefsd,
# which has been observed to block indefinitely on macOS 27 beta guests this
# early in boot (Setup Assistant itself was still stuck at the first-boot
# progress screen). Not seen on macOS 26 or 15 — treat as pre-release flakiness
# to route around, not something to root-cause here. Pure-bash so it works
# before any package manager is available. A killed function may leave an
# orphaned child process (e.g. a stuck `defaults write`) running harmlessly in
# the background — the goal is only to stop it from blocking configure.sh, and
# therefore Lima's boot-readiness gate, forever.
# Usage: run_with_timeout <seconds> <command-or-function> [args...]
run_with_timeout() {
    local secs="$1"; shift
    "$@" &
    local pid=$!
    ( sleep "$secs" 2>/dev/null; kill -9 "$pid" 2>/dev/null ) &
    local watcher=$!
    local rc=0
    wait "$pid" 2>/dev/null || rc=$?
    kill "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    return "$rc"
}

# ── Main Configuration Steps ───────────────────────────────────────────────────

main() {
    case "${1:-}" in
        runner)
            # Called by Makefile after macports.sh with RUNNER_LABEL and RUNNER_TOKEN set.
            # Bootstrap the setup-assistant suppressor first so it fires before runner install.
            local _sa_agent="$HOME/Library/LaunchAgents/com.lima.suppress-setup-assistant.plist"
            [[ -f "$_sa_agent" ]] && launchctl bootstrap gui/"$(id -u)" "$_sa_agent" 2>/dev/null || true
            configure_runner
            ;;
        autologin)
            # Re-apply auto-login after OS upgrades clear the setting.
            configure_password
            configure_autologin
            run_with_timeout 60 configure_screensaver || log_warn "configure_screensaver timed out or failed (non-fatal — see run_with_timeout comment)"
            ;;
        "")
            # Default: full provisioning run triggered by Lima at first boot.
            if ! [[ "$OSTYPE" == "darwin"* ]]; then
                log_error "This script is designed for macOS guests only"
                exit 1
            fi
            log_info "Starting macOS VM configuration..."
            configure_password
            configure_autologin
            run_with_timeout 60 configure_setup_assistant || log_warn "configure_setup_assistant timed out or failed (non-fatal — see run_with_timeout comment)"
            run_with_timeout 60 configure_screensaver || log_warn "configure_screensaver timed out or failed (non-fatal — see run_with_timeout comment)"
            configure_ssh_keys
            configure_chezmoi
            log_info "Configuration complete!"
            ;;
        *)
            log_error "Unknown subcommand: $1"
            exit 1
            ;;
    esac
}

# ── Screensaver and Sleep ─────────────────────────────────────────────────────

configure_screensaver() {
    log_info "Disabling screensaver and screen lock..."

    # Disable screensaver idle timer
    defaults -currentHost write com.apple.screensaver idleTime 0
    defaults write com.apple.screensaver idleTime 0
    sudo defaults write /Library/Preferences/com.apple.screensaver loginWindowIdleTime 0

    # Disable "require password after screensaver/display-off" — macOS 15 defaults this
    # to Immediately, which causes a lock screen on every display wake (including when
    # Lima.app connects to show the virtual display after reboot).
    defaults write com.apple.screensaver askForPassword -int 0
    defaults write com.apple.screensaver askForPasswordDelay -int 0
    defaults -currentHost write com.apple.screensaver askForPassword -int 0
    defaults -currentHost write com.apple.screensaver askForPasswordDelay -int 0

    # Disable sleep and energy saver features (not appropriate for a CI VM)
    sudo pmset -a sleep 0
    sudo pmset -a displaysleep 0
    sudo pmset -a disksleep 0
    sudo pmset -a standby 0
    sudo pmset -a powernap 0
    sudo pmset -a womp 0

    # Disable App Nap (process throttling when app is not frontmost)
    defaults write NSGlobalDomain NSAppSleepDisabled -bool YES


    log_info "Screensaver, screen lock, and energy saver settings disabled"
}

# ── Setup Assistant Suppression ───────────────────────────────────────────────

configure_setup_assistant() {
    log_info "Suppressing macOS Setup Assistant and first-run screens..."

    # Prevents the initial Setup Assistant wizard from running (system-level flags).
    # Lima touches both during disk patching, but re-touching is harmless and
    # guards against macOS 27 beta where Lima's patch doesn't write .skipbuddy.
    sudo touch /var/db/.AppleSetupDone
    sudo touch /var/db/.skipbuddy
    # /.resolve/33/private/var/run/.DidRunFLO is referenced in MiniLauncherPlugin binary;
    # touching it (in its canonical path) may satisfy an additional first-run check.
    sudo touch /private/var/run/.DidRunFLO 2>/dev/null || true

    # NOTE: SkipSetupItems (OSShowcase, Welcome, etc.) requires an MDM profile.
    # Apple blocked 'profiles install' via CLI since Big Sur (2020), and writing
    # directly to the managed preferences domain does not work for this key.
    #
    # Core DidSee* / MiniBuddy keys are written by Lima's fake-cloud-init
    # (suppressFirstLoginScreens in fakecloudinit_darwin.go) as a root LaunchDaemon
    # before the first GUI session — that is the correct timing to prevent macOS
    # from resetting them during first-login initialization.
    #
    # We also write user-level keys here (configure.sh runs via Lima SSH provisioning,
    # concurrently with the first GUI session). New DidSee* keys added for macOS 27
    # are safe no-ops on macOS 26 and earlier.
    # NOTE: On macOS 27, UAU's MiniLauncherPlugin sets MiniBuddyLaunchReason=13 during
    # the first autologin session (isNewUserAccount=true because PreviousBuildVersion is
    # absent from the plist). SA then keeps the plist at 13 while it shows the dialog.
    # The com.lima.sa-preseed root LaunchDaemon (installed below) fixes this by writing
    # PreviousBuildVersion + MiniBuddyLaunchReason=0 BEFORE loginwindow reads the pref.

    # Suppress analytics/diagnostics consent dialogs (Analytics screen appears on
    # first login in macOS 26+, even with .AppleSetupDone present).
    sudo /usr/bin/defaults write \
        "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory.plist" \
        AutoSubmit -bool false
    sudo /usr/bin/defaults write \
        "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory.plist" \
        ThirdPartyDataSubmit -bool false
    sudo /usr/bin/defaults write \
        "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory.plist" \
        AutoSubmitVersion -int 4
    sudo /usr/bin/defaults write \
        "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory.plist" \
        SeedAutoSubmit -bool false

    # ── macOS 27+ new setup assistant screens ────────────────────────────────
    # These DidSee* keys mark screens that are new or newly promoted in macOS 27.
    # Writing them true before the first GUI session prevents the dialogs from
    # appearing. Keys are ignored on macOS 26 and earlier.
    # Remove when: these screens are suppressed upstream in Lima's fake-cloud-init.
    local SETUP_PLIST="$HOME/Library/Preferences/com.apple.SetupAssistant.plist"
    # Full set of DidSee* keys observed in macOS 27 after manually dismissing all dialogs.
    # Keys that already existed in macOS 26 are safe no-ops on older releases.
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeAccessibility              -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeActivationLock             -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeAppStore                   -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeAppearanceSetup            -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeApplePaySetup              -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeCloudSetup                 -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeLockdownMode               -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeePrivacy                    -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeScreenTime                 -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeSetupSequence              -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeSiriSetup                  -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeSyncSetup                  -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeSyncSetup2                 -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeTermsOfAddress             -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeTouchIDSetup               -bool true
    /usr/bin/defaults write "$SETUP_PLIST" DidSeeiCloudLoginForStorageServices -bool true
    /usr/bin/defaults write "$SETUP_PLIST" MiniBuddyLaunchReason             -int 0
    /usr/bin/defaults write "$SETUP_PLIST" MiniBuddyLaunchedPostMigration    -bool false
    /usr/bin/defaults write "$SETUP_PLIST" MiniBuddyShouldLaunchToResumeSetup -bool false
    /usr/bin/defaults write "$SETUP_PLIST" SkipFirstLoginOptimization       -bool true
    /usr/bin/defaults write "$SETUP_PLIST" selectedFDEEscrowType            -string "DeclinedFDE"

    # Version-tagged keys suppress feature upsells introduced in macOS 27.
    # Using the current OS version so they stay suppressed across minor updates.
    local OS_VERSION BUILD_VERSION
    OS_VERSION=$(sw_vers -productVersion)
    BUILD_VERSION=$(sw_vers -buildVersion)
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenAgeRangeSelectionProductVersion -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenBuddyBuildVersion               -string "$BUILD_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenCloudProductVersion             -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenDiagnosticsProductVersion       -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenGlassTintUpsellProductVersion   -string "$OS_VERSION"

    # InitialSetup* keys are written by Setup Assistant when it completes first-time
    # setup. Without them, macOS 27 stamps MiniBuddyLaunchReason=13 on every login,
    # triggering the Apple Account dialog and blocking the desktop from loading.
    # Pre-seeding them tells macOS that initial setup was done for this OS version.
    /usr/bin/defaults write "$SETUP_PLIST" InitialSetupBuildVersion   -string "$BUILD_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" InitialSetupProductVersion -string "$OS_VERSION"

    # LastPreLoginTasksPerformed* tells bootinstalld / the pre-login task runner that its
    # tasks have already been executed for this build. Without these keys, macOS 27 fires
    # the post-DFU cleanup chain (bootinstalld → CleanupPreparePathService → mbsystemadministration
    # → mbuseragent → Setup Assistant "Software Update Complete") on every boot.
    /usr/bin/defaults write "$SETUP_PLIST" LastPreLoginTasksPerformedBuild   -string "$BUILD_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastPreLoginTasksPerformedVersion -string "$OS_VERSION"

    # PreviousSystemVersion / PreviousBuildVersion are read by loginwindow and passed
    # to UserAccountUpdater (UAU) as session args. When they are integer 0 (the default
    # for a fresh Lima user account), UAU's MiniLauncherPlugin sees previousOSVersion=nil
    # → isNewUserAccount=true → launches mini buddy with reason=13 (Apple Account dialog).
    # Setting them to the current version strings matches what SA writes after completing
    # setup, telling UAU this is a returning user and suppressing the new-user code path.
    # MiniBuddyLaunchCount records how many times mini buddy has already launched; 1 tells
    # UAU mini buddy completed its initial run.
    /usr/bin/defaults write "$SETUP_PLIST" PreviousSystemVersion -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" PreviousBuildVersion  -string "$BUILD_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" MiniBuddyLaunchCount  -int 1

    # macOS 27: UAU MiniLauncherPlugin (com.apple.MiniBuddyLauncher) in UserAccountUpdater
    # decides whether to launch mini buddy with reason=13 ("new user account needing Apple
    # Account"). It checks isNewUserAccount which depends on two MBPerUserState properties:
    #   isInitialAccountOnMac (plist key: InitialAccountOnMac) — whether this is an established
    #     initial Mac account (true) vs a new account (false → triggers reason=13 launch)
    #   initialAccountSetupDate (plist key: InitialAccountSetupDate) — date when account was
    #     set up; nil means "new account" regardless of InitialAccountOnMac value
    # Both must be set to prevent UAU from treating the VM user as a "new user account."
    # SkipiCloudSetup / SkipiCloudStorageSetup skip those specific mini buddy panels in
    # case mini buddy launches anyway (defense-in-depth).
    /usr/bin/defaults write "$SETUP_PLIST" InitialAccountOnMac -bool true
    /usr/bin/plutil -replace InitialAccountSetupDate -date "2026-01-01T00:00:00Z" "$SETUP_PLIST" 2>/dev/null || \
        /usr/bin/plutil -insert  InitialAccountSetupDate -date "2026-01-01T00:00:00Z" "$SETUP_PLIST" 2>/dev/null || true
    /usr/bin/defaults write "$SETUP_PLIST" SkipiCloudSetup         -bool true
    /usr/bin/defaults write "$SETUP_PLIST" SkipiCloudStorageSetup  -bool true

    # macOS 27 post-DFU: write skip keys to the MANAGED plist so mbuseragent (the
    # user-space agent in the bootinstalld post-DFU chain) respects them before
    # launching Setup Assistant. The managed plist is authoritative over the user
    # plist. macOS 15+ uses SkipSetupItems (array) instead of individual boolean
    # keys; UpdateCompleted specifically suppresses the post-DFU "Software Update
    # Complete" SA chain triggered by bootinstalld on every first-GUI-session boot.
    local MANAGED_SA_PLIST="/Library/Preferences/com.apple.SetupAssistant.managed.plist"
    sudo /usr/bin/defaults write "$MANAGED_SA_PLIST" MiniBuddyLaunchReason      -int 0
    sudo /usr/bin/defaults write "$MANAGED_SA_PLIST" SkipExpressSettingsUpdating -bool true
    sudo /usr/bin/defaults write "$MANAGED_SA_PLIST" SkipiCloudSetup             -bool true
    sudo /usr/bin/defaults write "$MANAGED_SA_PLIST" SkipiCloudStorageSetup      -bool true
    # SkipSetupItems array (macOS 15+): used by SA / mbuseragent in the bootinstalld
    # post-DFU chain. UpdateCompleted suppresses the "Software Update Complete" pane;
    # AppleID suppresses the Apple Account sign-in screen. Other entries suppress
    # screens that would otherwise appear on a fresh DFU-restored boot.
    sudo /usr/bin/defaults write "$MANAGED_SA_PLIST" SkipSetupItems -array \
        "AppleID" \
        "Diagnostics" \
        "FileVault" \
        "Intelligence" \
        "SoftwareUpdate" \
        "UpdateCompleted" \
        "Welcome"

    # macOS 27: ISRootMigrator (in UserAccountUpdater) reads AppleLanguagesSchemaVersion
    # from NSGlobalDomain via cfprefsd. When zero/missing on first boot, it sets
    # isNewUserAccount=1 → MiniLauncherPlugin launches SA with reason 13 (Apple Account).
    # cfprefsd does not persist NSGlobalDomain writes from non-GUI sessions (Boot 1 SSH
    # provision) to .GlobalPreferences.plist. Write the file directly with PlistBuddy so
    # cfprefsd reads 5400 from disk when Boot 2's GUI session starts (cfprefsd agent
    # initialises from the plist on disk at the first GUI login).
    local MAJOR_VERSION
    MAJOR_VERSION=$(sw_vers -productVersion | cut -d. -f1)
    if [ "${MAJOR_VERSION}" -ge 27 ] 2>/dev/null; then
        local GLOBAL_PREFS="$HOME/Library/Preferences/.GlobalPreferences.plist"
        /usr/libexec/PlistBuddy -c "Set :AppleLanguagesSchemaVersion 5400" "$GLOBAL_PREFS" 2>/dev/null || \
            /usr/libexec/PlistBuddy -c "Add :AppleLanguagesSchemaVersion integer 5400" "$GLOBAL_PREFS"
        chmod 600 "$GLOBAL_PREFS"
    fi

    # Additional version-tagged keys for macOS 27 features.
    # Setting these to the current OS version prevents upsell/feature dialogs from appearing.
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenIntelligenceProductVersion          -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenNewFeaturesProductVersion           -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenSiriProductVersion                  -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenStorageServicesProductVersion       -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeeniCloudStorageServicesProductVersion -string "$OS_VERSION"
    /usr/bin/defaults write "$SETUP_PLIST" LastSeenSyncProductVersion                  -string "$OS_VERSION"

    # New in macOS 27: separate plist for the setup assistant privacy pane.
    # Without this, the Privacy & Security setup screen re-appears on first login.
    local PRIVACY_PLIST="$HOME/Library/Preferences/com.apple.setupassistant.privacypane.plist"
    /usr/bin/defaults write "$PRIVACY_PLIST" HasSeenPrivacy        -bool true
    /usr/bin/defaults write "$PRIVACY_PLIST" LastSeenPrivacyVersion -int 2
    /usr/bin/defaults write "$PRIVACY_PLIST" MigrationVersion       -int 1

    # Belt-and-suspenders: LaunchAgent that resets MiniBuddyLaunchReason=0 at
    # login in case anything re-stamps it after the sa-preseed daemon ran.
    # The primary suppression mechanism is the managed plist (written above by
    # suppress_setup_assistant() and refreshed by sa-preseed on every boot).
    local LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LAUNCH_AGENTS_DIR"
    local SUPPRESS_SA_SCRIPT="$HOME/Library/Scripts/lima-suppress-sa.sh"
    mkdir -p "$(dirname "$SUPPRESS_SA_SCRIPT")"
    cat > "$SUPPRESS_SA_SCRIPT" << 'SCRIPT'
#!/bin/sh
SA_PLIST="$HOME/Library/Preferences/com.apple.SetupAssistant.plist"
/usr/bin/defaults write "$SA_PLIST" MiniBuddyLaunchReason              -int 0 2>/dev/null || true
/usr/bin/defaults write "$SA_PLIST" MiniBuddyShouldLaunchToResumeSetup -bool false 2>/dev/null || true
SCRIPT
    chmod 755 "$SUPPRESS_SA_SCRIPT"

    cat > "$LAUNCH_AGENTS_DIR/com.lima.suppress-setup-assistant.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lima.suppress-setup-assistant</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>${SUPPRESS_SA_SCRIPT}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
    launchctl load "$LAUNCH_AGENTS_DIR/com.lima.suppress-setup-assistant.plist" 2>/dev/null || true

    # Root LaunchDaemon that pre-seeds the SetupAssistant plist BEFORE loginwindow reads it.
    # Problem: UAU's MiniLauncherPlugin sets MiniBuddyLaunchReason=13 during the first
    # autologin session (because PreviousBuildVersion is absent → isNewUserAccount=true).
    # SA then keeps the plist at 13 while it shows its dialog. On the next boot, loginwindow
    # reads 13 → "MiniBuddyLaunch pref is set" → SA re-launches before configure.sh can run.
    # Fix: run a root LaunchDaemon (before any GUI session) to write MiniBuddyLaunchReason=0
    # and PreviousBuildVersion=current_build. With PreviousBuildVersion set, UAU gets a
    # matching previous build → isNewUserAccount=false → MiniBuddyLaunchReason stays 0.
    local SA_PRESEED_SCRIPT="/usr/local/sbin/lima-sa-preseed.sh"
    local SA_PRESEED_PLIST="/Library/LaunchDaemons/com.lima.sa-preseed.plist"
    local USER_SA_PLIST="$HOME/Library/Preferences/com.apple.SetupAssistant.plist"
    local SA_GUEST_UID SA_GUEST_GID
    SA_GUEST_UID=$(/usr/bin/id -u) || return 1
    SA_GUEST_GID=$(/usr/bin/id -g) || return 1
    # Capture the configured user now; this generated script runs as root at boot.
    [[ "$SA_GUEST_UID" =~ ^[0-9]+$ && "$SA_GUEST_GID" =~ ^[0-9]+$ ]] || return 1
    sudo tee "$SA_PRESEED_SCRIPT" > /dev/null << SCRIPT
#!/bin/sh
BUILD=\$(sw_vers -buildVersion 2>/dev/null)
VERSION=\$(sw_vers -productVersion 2>/dev/null)
[ -n "\$BUILD" ]   || exit 0
[ -n "\$VERSION" ] || exit 0
MAJOR_VERSION=\$(echo "\$VERSION" | cut -d. -f1)
if [ "\$MAJOR_VERSION" -ge 27 ] 2>/dev/null; then
    # macOS 27: ISRootMigrator reads AppleLanguagesSchemaVersion from cfprefsd (NSGlobalDomain).
    # cfprefsd agent initialises from .GlobalPreferences.plist at user-session start.
    # This daemon runs before the user session, so writing here before the agent starts
    # ensures the agent caches 5400 and ISRootMigrator does not trigger the SA Apple Account dialog.
    # The SA plist may not exist yet (it's written by configure.sh later via SSH), so this
    # block intentionally runs before the SA plist check below.
    GUEST_UID="${SA_GUEST_UID}"
    GUEST_GID="${SA_GUEST_GID}"
    if [ -n "\$GUEST_UID" ]; then
        GLOBAL_PREFS="$HOME/Library/Preferences/.GlobalPreferences.plist"
        /usr/libexec/PlistBuddy -c "Set :AppleLanguagesSchemaVersion 5400" "\$GLOBAL_PREFS" 2>/dev/null || \
            /usr/libexec/PlistBuddy -c "Add :AppleLanguagesSchemaVersion integer 5400" "\$GLOBAL_PREFS" 2>/dev/null || true
        [ -f "\$GLOBAL_PREFS" ] && chown "\${GUEST_UID}:\${GUEST_GID}" "\$GLOBAL_PREFS" 2>/dev/null || true
        [ -f "\$GLOBAL_PREFS" ] && chmod 600 "\$GLOBAL_PREFS" 2>/dev/null || true
    fi
fi
PLIST="${USER_SA_PLIST}"
[ -f "\$PLIST" ]   || exit 0
/usr/bin/defaults write "\$PLIST" MiniBuddyLaunchReason -int 0
/usr/bin/defaults write "\$PLIST" MiniBuddyLaunchedPostMigration -bool false
/usr/bin/defaults write "\$PLIST" MiniBuddyShouldLaunchToResumeSetup -bool false
/usr/bin/defaults write "\$PLIST" PreviousBuildVersion -string "\$BUILD"
/usr/bin/defaults write "\$PLIST" PreviousSystemVersion -string "\$VERSION"
/usr/bin/defaults write "\$PLIST" LastSeenBuddyBuildVersion -string "\$BUILD"
# Write skip keys to the managed plist so mbuseragent / Setup Assistant respect
# them before showing any post-DFU dialogs. The managed plist is authoritative
# over the user plist for these keys. This daemon runs as root before any GUI
# session, so it can write to /Library/Preferences/.
MANAGED_PLIST="/Library/Preferences/com.apple.SetupAssistant.managed.plist"
/usr/bin/defaults write "\$MANAGED_PLIST" MiniBuddyLaunchReason      -int 0
/usr/bin/defaults write "\$MANAGED_PLIST" SkipExpressSettingsUpdating -bool true
/usr/bin/defaults write "\$MANAGED_PLIST" SkipiCloudSetup             -bool true
/usr/bin/defaults write "\$MANAGED_PLIST" SkipiCloudStorageSetup      -bool true
/usr/bin/defaults write "\$MANAGED_PLIST" SkipSetupItems -array \
    "AppleID" \
    "Diagnostics" \
    "FileVault" \
    "Intelligence" \
    "SoftwareUpdate" \
    "UpdateCompleted" \
    "Welcome"
# Restore plist ownership: defaults write (root) atomically replaces the file,
# changing ownership to root. cfprefsd agent (running as the user) cannot read
# a root-owned 0600 file — it gets an empty SA domain and mini-buddy shows dialogs.
GUEST_UID="${SA_GUEST_UID}"
GUEST_GID="${SA_GUEST_GID}"
[ -n "\$GUEST_UID" ] && chown "\${GUEST_UID}:\${GUEST_GID}" "\$PLIST" 2>/dev/null || true
SCRIPT
    sudo chmod 755 "$SA_PRESEED_SCRIPT"
    sudo chown root:wheel "$SA_PRESEED_SCRIPT"
    sudo tee "$SA_PRESEED_PLIST" > /dev/null << 'LAUNCHDAEMON'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lima.sa-preseed</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/sbin/lima-sa-preseed.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
LAUNCHDAEMON
    sudo chmod 644 "$SA_PRESEED_PLIST"
    sudo chown root:wheel "$SA_PRESEED_PLIST"
    sudo launchctl bootstrap system "$SA_PRESEED_PLIST" 2>/dev/null || true

    # macOS 27: /private/tmp/.AppleMiniSetupDidRun records that mini buddy ran for UID 501.
    # MiniLauncherPlugin checks this file on each login. /private/tmp/ is a volatile tmpfs
    # cleared on every reboot, so mini buddy re-appears after reboot unless this file is
    # recreated. A root LaunchDaemon recreates it at each boot, before user login.
    local MINI_SETUP_DONE_PLIST="/Library/LaunchDaemons/com.lima.mini-setup-done.plist"
    sudo tee "$MINI_SETUP_DONE_PLIST" > /dev/null << 'LAUNCHDAEMON'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lima.mini-setup-done</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>printf '{"uids":[501]}' > /private/tmp/.AppleMiniSetupDidRun; chmod 644 /private/tmp/.AppleMiniSetupDidRun</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
LAUNCHDAEMON
    sudo chmod 644 "$MINI_SETUP_DONE_PLIST"
    sudo chown root:wheel "$MINI_SETUP_DONE_PLIST"
    # Create the file immediately too, for the current boot session.
    printf '{"uids":[501]}' > /private/tmp/.AppleMiniSetupDidRun 2>/dev/null || \
        sudo sh -c 'printf '"'"'{"uids":[501]}'"'"' > /private/tmp/.AppleMiniSetupDidRun; chmod 644 /private/tmp/.AppleMiniSetupDidRun' 2>/dev/null || true
    # Load the daemon into the system launchd domain so it fires on every subsequent boot.
    sudo launchctl bootstrap system "$MINI_SETUP_DONE_PLIST" 2>/dev/null || true

    log_info "Setup Assistant suppression complete"
}

# ── Auto-Login Configuration ──────────────────────────────────────────────────

# Encode a password into /etc/kcpassword format and write it as root.
# Uses only bash built-ins + xxd (/usr/bin/xxd ships with macOS, no CLT needed).
# Compatible with bash 3.2 (macOS system shell at /bin/bash).
# Algorithm: XOR each password byte with the 11-byte cipher key, then pad to the
# next multiple of 12 (always ≥1 byte) so macOS finds a null terminator on decode.
# Ref: https://github.com/brunerd/macAdminTools/blob/main/Scripts/setAutoLogin.sh
_write_kcpassword() {
    local pw="$1"
    local cipher_hex=(7D 89 52 23 D2 BC DD EA A3 B9 1F)

    # Convert password string to an array of uppercase hex byte values.
    local pw_hex_array
    pw_hex_array=( $( printf '%s' "$pw" | xxd -p -u | sed 's/../& /g' ) )

    local pw_len=${#pw_hex_array[@]}

    # Pad to next multiple of 12, always at least 1 extra byte.
    local padding
    if [ "$pw_len" -lt 12 ]; then
        padding=$(( 12 - pw_len ))
    elif [ $(( pw_len % 12 )) -ne 0 ]; then
        padding=$(( 12 - pw_len % 12 ))
    else
        padding=12
    fi

    local total=$(( pw_len + padding ))
    local out_hex="" i ch_hex ci_hex xor_byte

    for (( i=0; i<total; i++ )); do
        ch_hex="${pw_hex_array[$i]:-00}"
        ci_hex="${cipher_hex[$(( i % 11 ))]}"
        xor_byte=$( printf "%02X" "$(( 0x${ci_hex} ^ 0x${ch_hex} ))" )
        out_hex="${out_hex}${xor_byte}"
    done

    # Write binary via sudo tee — avoids shell-redirect permission issues.
    printf '%s' "$out_hex" | xxd -r -p | sudo tee /etc/kcpassword > /dev/null
    sudo chmod 0600 /etc/kcpassword
    sudo chown root:wheel /etc/kcpassword
}

configure_autologin() {
    if [[ "${AUTO_LOGIN:-1}" == "0" ]]; then
        log_info "AUTO_LOGIN=0; skipping auto-login configuration"
        return 0
    fi

    local current_user password_file password_val
    current_user=$(whoami)

    # configure_password runs first so ~/password always reflects the current password.
    password_file="$HOME/password"
    if [[ ! -f "$password_file" ]]; then
        log_warn "~/password not found; skipping auto-login (Lima password file missing)"
        return 0
    fi

    log_info "Enabling automatic login for $current_user"

    set +x
    password_val=$(cat "$password_file")

    # Attempt 1: sysadminctl -autologin set (available since macOS Ventura).
    # Handles both autoLoginUser preference and /etc/kcpassword in one call.
    # KNOWN LIMITATION: SACSetAutoLoginPassword internally calls the Security Agent,
    # which requires an active GUI/login-window context. When run from SSH or any
    # headless root context (no Security Agent), it fails silently with error:22
    # (EINVAL) and exits 0 without creating kcpassword. This affects macOS 15+.
    # Reference: https://derflounder.wordpress.com/2023/03/04/setting-a-user-account-to-automatically-log-in-using-sysadminctl-on-macos-ventura/
    sudo sysadminctl \
        -autologin set \
        -userName "$current_user" \
        -password "$password_val" \
        >/dev/null 2>&1

    # Always write autoLoginUser — sysadminctl may create kcpassword but silently
    # fail to write the loginwindow preference when called from SSH without a
    # Security Agent context (macOS 15+, error:22 / EINVAL).
    sudo defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser "$current_user"

    # Always (re)write kcpassword ourselves: a file left by Apple's guest provisioning or an
    # earlier run holds the previous password, and sysadminctl above does not replace it
    # without a Security Agent context. A stale kcpassword makes loginwindow refuse the
    # automatic login ("Your password is required to log in").
    # Validate the password first so a wrong one never gets written.
    if ! /usr/bin/dscl /Search -authonly "$current_user" "$password_val" &>/dev/null; then
        log_warn "Password in ~/password does not authenticate — kcpassword not written"
        log_warn "Auto-login will not work; re-run configure.sh after correcting the password"
        set -x
        return 0
    fi

    _write_kcpassword "$password_val"

    set -x
    log_info "Auto-login enabled — VM will log in as '$current_user' on next boot"
    log_warn "Login screen is bypassed; only use this in a trusted/private environment"
}

# ── SSH Key Configuration ──────────────────────────────────────────────────────

configure_ssh_keys() {
    if [[ -z "${SSH_PUBLIC_KEY:-}" ]]; then
        log_info "SSH_PUBLIC_KEY not set; using Lima's default key injection (all ~/.ssh/*.pub)"
        return 0
    fi

    log_info "SSH_PUBLIC_KEY set; replacing user keys in authorized_keys with specified key"
    log_info "(Lima's own operational key is preserved for limactl SSH access)"

    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"

    # Preserve Lima's own key (comment "lima") — required for limactl shell / file sharing.
    # Replace all other entries with only the specified key.
    local lima_key=""
    if [[ -f "$HOME/.ssh/authorized_keys" ]]; then
        lima_key=$(grep ' lima$' "$HOME/.ssh/authorized_keys" || true)
    fi

    set +x
    {
        echo "$SSH_PUBLIC_KEY"
        [[ -n "$lima_key" ]] && echo "$lima_key"
    } > "$HOME/.ssh/authorized_keys"
    set -x
    chmod 600 "$HOME/.ssh/authorized_keys"

    log_info "authorized_keys set: specified key + Lima operational key"
}

# ── Password Configuration ─────────────────────────────────────────────────────

configure_password() {
    log_info "Configuring macOS user password..."
    
    if [[ -z "${MACOS_PASSWORD:-}" ]]; then
        log_warn "MACOS_PASSWORD not set; keeping Lima-generated password (see ~/password)"
        return 0
    fi
    
    local current_user
    current_user=$(whoami)
    local password_file="$HOME/password"
    
    if [[ ! -f "$password_file" ]]; then
        log_warn "Lima password file ($password_file) not found; skipping password change"
        return 0
    fi
    
    local old_password
    set +x  # keep the password out of the xtrace log
    old_password=$(cat "$password_file")
    set -x
    
    log_info "Setting password for user: $current_user"
    # Use sysadminctl -resetPasswordFor which works with Secure Token accounts.
    # The Lima-generated old password is used as the admin credential since the
    # guest user is the only admin. Note: this does not update the login keychain.
    # Disable errexit and trace to avoid leaking secrets in logs.
    set +ex
    sudo sysadminctl \
        -adminUser "$current_user" \
        -adminPassword "$old_password" \
        -resetPasswordFor "$current_user" \
        -newPassword "$MACOS_PASSWORD" \
        >/dev/null 2>&1
    local rc=$?
    set -ex

    if [[ $rc -eq 0 ]]; then
        set +x
        chmod u+w "$password_file"
        echo "$MACOS_PASSWORD" > "$password_file"
        chmod 400 "$password_file"
        set -x
        log_info "Password configured successfully (keychain unchanged — update manually if needed)"
    else
        log_warn "Password change failed (rc=$rc)"
        log_warn "Change it manually via: passwd  OR  System Settings > Users & Groups"
        log_warn "Current generated password: cat ~/password"
    fi
}

# ── Chezmoi Dotfiles ──────────────────────────────────────────────────────────

configure_chezmoi() {
    log_info "Configuring dotfiles via chezmoi..."

    # Skip if explicitly requested (useful for debugging or fast VM rebuilds)
    if [[ "${SKIP_CHEZMOI:-0}" == "1" ]]; then
        log_warn "Skipping chezmoi configuration (SKIP_CHEZMOI=1)"
        return 0
    fi

    local install_script="/tmp/lima-chezmoi-install.sh"
    local log_file="/tmp/lima-chezmoi.log"

    # Download the chezmoi installer to a file.
    # Using -o rather than command substitution so we can detect failures
    # and avoid passing an empty string to sh -c.
    if ! curl -fsLS get.chezmoi.io -o "$install_script"; then
        log_warn "Failed to download chezmoi installer (network not yet ready?)"
        log_warn "Re-run configure.sh manually after boot: SKIP_CHEZMOI=0 /Volumes/lima_mac/configure.sh"
        return 0
    fi
    chmod +x "$install_script"

    # Run chezmoi in the background so this script returns quickly — Lima's boot.sh
    # writes lima-boot-done after this script exits, and limactl start waits for
    # that marker within a 600-second timeout. A full chezmoi bootstrap (MacPorts
    # + dotfiles) takes 15-20 minutes.
    #
    # Use a subshell with stdin/stdout/stderr fully redirected so the process
    # survives after configure.sh exits (no terminal, no SIGHUP from parent).
    # Progress: limactl shell macos-26 tail -f $log_file
    (
        exec < /dev/null
        exec >> "$log_file" 2>&1
        "$install_script" init --apply trodemaster --use-builtin-git true
    ) &
    local bg_pid=$!
    disown "$bg_pid"

    log_info "Chezmoi running in background (pid $bg_pid)"
    log_info "Monitor progress: limactl shell macos-26 tail -f $log_file"
}

# ── GitHub Actions Runner ─────────────────────────────────────────────────────
#
# Downloads, extracts, configures, and installs the GitHub Actions runner service.
# Called by the Makefile after macports.sh — not during Lima provisioning.
#
# The registration token is generated on the host via `gh api` and passed in;
# no GitHub authentication is needed inside the VM.
#
# Required env vars (set by Makefile, not secrets):
#   RUNNER_LABEL   — runner name/label (e.g. macOS_26)
#   RUNNER_TOKEN   — one-time registration token from GitHub API (expires in 1h)
#
# Optional env vars:
#   GITHUB_OWNER   — defaults to trodemaster
#   GITHUB_REPO    — defaults to blakeports

configure_runner() {
    log_info "Configuring GitHub Actions runner..."

    if [[ -z "${RUNNER_LABEL:-}" ]]; then
        log_warn "RUNNER_LABEL not set; skipping runner configuration"
        return 0
    fi

    if [[ -z "${RUNNER_TOKEN:-}" ]]; then
        log_warn "RUNNER_TOKEN not set; skipping runner configuration"
        return 0
    fi

    local owner="${GITHUB_OWNER:-trodemaster}"
    local repo="${GITHUB_REPO:-blakeports}"
    local runner_labels="self-hosted,macOS,ARM64,${RUNNER_LABEL}"

    # Skip if already configured on disk (idempotent guard)
    if [[ -f /opt/actions-runner/.runner ]]; then
        log_info "Runner already configured at /opt/actions-runner — skipping"
        return 0
    fi

    # Determine latest stable runner version
    local runner_version
    runner_version=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
        | jq -r '.tag_name | ltrimstr("v")' 2>/dev/null)

    if [[ -z "$runner_version" ]]; then
        log_warn "Failed to determine runner version; skipping runner setup"
        return 0
    fi
    log_info "Runner version: ${runner_version}"

    local arch
    arch=$(uname -m)
    local runner_package
    case "$arch" in
        "arm64")  runner_package="actions-runner-osx-arm64-${runner_version}.tar.gz" ;;
        "x86_64") runner_package="actions-runner-osx-x64-${runner_version}.tar.gz" ;;
        *)
            log_warn "Unsupported arch $arch; skipping runner setup"
            return 0
            ;;
    esac

    local runner_url="https://github.com/actions/runner/releases/download/v${runner_version}/${runner_package}"
    log_info "Downloading runner: ${runner_package}"
    if ! curl -fsSL -o /tmp/actions-runner.tar.gz "$runner_url"; then
        log_warn "Failed to download runner package; skipping"
        return 0
    fi

    # Extract and take ownership
    sudo mkdir -p /opt/actions-runner
    sudo tar -xzf /tmp/actions-runner.tar.gz -C /opt/actions-runner
    sudo chown -R "$(whoami):$(id -gn)" /opt/actions-runner
    rm -f /tmp/actions-runner.tar.gz

    # Configure the runner using the pre-generated registration token
    log_info "Configuring runner '${RUNNER_LABEL}' with labels: ${runner_labels}"
    set +x
    cd /opt/actions-runner
    ./config.sh \
        --url "https://github.com/${owner}/${repo}" \
        --token "${RUNNER_TOKEN}" \
        --name "${RUNNER_LABEL}" \
        --labels "${runner_labels}" \
        --unattended --replace
    local rc=$?
    set -x

    if [[ $rc -ne 0 ]]; then
        log_warn "Runner configuration failed (rc=$rc)"
        return 0
    fi

    # Install the LaunchAgent service
    log_info "Installing runner LaunchAgent service..."
    ./svc.sh install

    # Attempt to start the service. LaunchAgents require the gui/<uid> launchd domain,
    # which only exists after the user logs into the macOS desktop. If auto-login is
    # enabled (configure_autologin), the domain will be present on next boot and the
    # agent will start automatically. We attempt bootstrap here in case a GUI session
    # already exists (e.g. re-runs of configure.sh), and silently continue if not.
    local plist
    plist=$(ls "$HOME"/Library/LaunchAgents/actions.runner.*.plist 2>/dev/null | head -1 || true)
    if [[ -n "$plist" ]]; then
        local uid
        uid=$(id -u)
        if launchctl bootstrap "gui/${uid}" "$plist" 2>/dev/null; then
            log_info "Runner service started (gui/${uid})"
        else
            log_warn "Runner service installed but not started (no active GUI session)"
            log_warn "The service will start automatically on the next GUI login"
        fi
    fi

    log_info "Runner '${RUNNER_LABEL}' configured and service installed"
}

# ── Execution ──────────────────────────────────────────────────────────────────

main "$@"
