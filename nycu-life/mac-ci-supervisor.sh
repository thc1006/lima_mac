#!/bin/bash
# nycu-life macOS CI supervisor — per-job ephemeral CoW clone from a clean golden.
#
# Model (the review's "每 job 全新 clone"): the golden `macos-ci` is Xcode-only, clean,
# STOPPED. Per queued nycu-life-macos job: CoW-clone the golden (instant, ~0 disk),
# boot the clone, run ONE job via a JIT (just-in-time, auto-removing) runner, then
# DESTROY the clone. No persistent runner identity or signing secrets in the golden.
#
# Admission control: pause while a human uses the Mac, or when disk is low.
# Reconcile: on start, delete any orphan macos-job-* clones left by a crash.
#
# Requires on the host (chmod 600):
#   ~/ci/.gh_token   fine-grained PAT for nycu-life/super-app: Actions:read + Administration:read/write
#   ~/ci/.kc_pw      the Mac login password (to unlock the host login keychain for vz HostKey);
#                    optional if auto-login already keeps the keychain unlocked.
set -u
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin

GOLDEN="${GOLDEN:-macos-ci}"
REPO="${REPO:-nycu-life/super-app}"
LABEL="${LABEL:-nycu-life-macos}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/ci/.gh_token}"
KC_PW_FILE="${KC_PW_FILE:-$HOME/ci/.kc_pw}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
SUPERVISOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLL="${POLL:-15}"
MIN_FREE_GB="${MIN_FREE_GB:-15}"
IDLE_SECS="${IDLE_SECS:-300}"
DRAIN_FILE="${DRAIN_FILE:-$HOME/ci/maintenance.drain}"

log() { echo "$(date '+%H:%M:%S') $*"; }
gh_api() { GH_TOKEN="$(cat "$TOKEN_FILE")" gh api "$@"; }
draining() { [ -e "$DRAIN_FILE" ] || [ -L "$DRAIN_FILE" ]; }

human_active() {
  # "using the Mac" = recent physical HID input (keyboard/mouse). A logged-in-but-idle GUI
  # session does NOT count — we actually want a login present (keychain/sim), just not someone
  # actively typing. Only recent input (idle < IDLE_SECS) pauses CI admission.
  local idle
  idle=$(ioreg -c IOHIDSystem 2>/dev/null | awk -F= '/HIDIdleTime/{print int($2/1000000000); exit}')
  [ -n "$idle" ] && [ "$idle" -lt "$IDLE_SECS" ] || return 1
  # Ignore a low idle reading in the first IDLE_SECS after boot: right after auto-login the HID
  # idle counter reads ~uptime, which would otherwise falsely pause CI for ~5 min after a reboot.
  local now boot
  now=$(date +%s); boot=$(sysctl -n kern.boottime 2>/dev/null | awk '{print $4}' | tr -d ',')  # field 4 = boot epoch sec
  [ -n "$boot" ] && [ $((now - boot)) -gt "$IDLE_SECS" ]
}
host_unhealthy() {
  local g; g=$(df -g / | awk 'NR==2{print $4}')
  [ "${g:-99}" -lt "$MIN_FREE_GB" ]
}
unlock_keychain() {
  [ -f "$KC_PW_FILE" ] || return 1
  python3 "$SUPERVISOR_DIR/unlock-keychain.py" "$KC_PW_FILE" "$KEYCHAIN"; local rc=$?
  [ "$rc" = 0 ] || log "[unlock] FAILED rc=$rc"
  return $rc
}

queued_macos_job() {
  # Must scan all NON-completed runs, not just status=queued: in a multi-job run the
  # Linux jobs make the run "in_progress" while the iOS job is still "queued" waiting
  # for our label. Looking only at queued runs would miss (and strand) those iOS jobs.
  # Capture the jq output and match only when NON-EMPTY. `... | head -1 && return 0` is wrong:
  # head always exits 0, so it returns "found" for the first in-progress run even when NO
  # nycu-life-macos job is queued there -> provisions a phantom clone that idles forever.
  local rid out
  for rid in $(gh_api "repos/$REPO/actions/runs?per_page=40" \
                 --jq '.workflow_runs[]|select(.status!="completed")|.id' 2>/dev/null); do
    out=$(gh_api "repos/$REPO/actions/runs/$rid/jobs" \
            --jq ".jobs[]|select(.status==\"queued\")|select((.labels//[])|index(\"$LABEL\"))|.name" 2>/dev/null | head -1)
    [ -n "$out" ] && { echo "$out"; return 0; }
  done
  return 1
}

reconcile() {
  local inst
  for inst in $(limactl list --format '{{.Name}}' 2>/dev/null | grep '^macos-job-'); do
    log "[reconcile] destroying orphan $inst"
    limactl stop -f "$inst" 2>/dev/null; limactl delete -f "$inst" 2>/dev/null
  done
}

provision_and_run() {
  local id inst jit
  id=$(date +%Y%m%d%H%M%S)
  inst="macos-job-$id"
  # Recheck after the GitHub lookup: maintenance may have begun during polling.
  if draining; then log "[admission] drain — skip queued job"; return 0; fi
  log "[provision] clone $GOLDEN -> $inst (CoW)"
  limactl clone "$GOLDEN" "$inst" >/dev/null 2>&1 || { log "clone failed"; return 1; }
  unlock_keychain
  limactl start --timeout 90s "$inst" >/dev/null 2>&1 || { log "start failed"; limactl delete -f "$inst" 2>/dev/null; return 1; }
  jit=$(gh_api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f name="$inst" -F runner_group_id=1 -f "labels[]=$LABEL" -f work_folder=_work \
        --jq '.encoded_jit_config' 2>/dev/null)
  if [ -z "$jit" ]; then log "jitconfig failed"; limactl stop -f "$inst" 2>/dev/null; limactl delete -f "$inst" 2>/dev/null; return 1; fi
  log "[run] one job in $inst"
  limactl shell "$inst" -- bash -lc "cd ~/actions-runner && ./run.sh --jitconfig '$jit'" 2>&1 | sed "s/^/  [$inst] /"
  log "[cleanup] destroy $inst"
  limactl stop -f "$inst" 2>/dev/null; limactl delete -f "$inst" 2>/dev/null
}

log "supervisor starting (golden=$GOLDEN repo=$REPO label=$LABEL)"
# A maintenance request survives reboot. Do not reconcile or unlock anything
# until the operator has verified the host and removed the request.
while draining; do log "[admission] drain — startup paused"; sleep "$POLL"; done
reconcile
if unlock_keychain; then log "[startup] keychain unlocked"; fi   # unlock at startup too (reboot), not only per-job
while true; do
  if draining; then log "[admission] drain — idle"; sleep "$POLL"; continue; fi
  if host_unhealthy; then log "[admission] low disk — pause"; sleep 60; continue; fi
  if human_active;  then log "[admission] human active — pause"; sleep 60; continue; fi
  if queued_macos_job >/dev/null; then provision_and_run; else sleep "$POLL"; fi
done
