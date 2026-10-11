# Lima macOS VM Management
# Manages four VM instances: macos-27, macos-26, macos-15 (N-2), macos-14 (N-3)
# plus test VMs: macos-26-test, macos-15-test

.PHONY: build-26 patch-26 clean-26 rebuild-26 runner-26 \
        build-27 patch-27 clean-27 rebuild-27 runner-27 \
        ci-netatalk-27 \
        build-15 patch-15 clean-15 rebuild-15 runner-15 \
        build-14 patch-14 clean-14 rebuild-14 runner-14 \
        build-26-test clean-26-test rebuild-26-test \
        build-15-test clean-15-test rebuild-15-test \
        status help

# ── Tool paths ────────────────────────────────────────────────────────────────

LIMACTL         ?= limactl
LIMA_APP_BUNDLE ?= /Applications/MacPorts/Lima.app
GHRUNNER        ?= $(HOME)/Developer/blakeports/scripts/ghrunner

# ── GitHub repository ─────────────────────────────────────────────────────────

GITHUB_OWNER    ?= trodemaster
GITHUB_REPO     ?= blakeports

# ── Build options ─────────────────────────────────────────────────────────────

# Set to 1 to skip OS software update check (speeds up test builds)
SKIP_OS_UPDATE  ?= 0

# Xcode .xip (a filename in lima_mac/xcode/) installed on each VM. Pick the Xcode that
# matches the guest; override one on the command line, e.g. `make build-26 XCODE_XIP_26=`
# (empty = skip Xcode, CLT only). A missing file is skipped with a warning.
XCODE_XIP_26       ?= Xcode_27.xip
XCODE_XIP_27       ?= Xcode_27.xip
XCODE_XIP_15       ?= Xcode_26.3_Universal.xip
XCODE_XIP_14       ?=

# Host-side guest customization tool (Go, no cgo); built on demand.
MACOS_SETUP     := $(CURDIR)/tools/macos-setup/macos-setup
$(MACOS_SETUP): $(wildcard tools/macos-setup/*.go) tools/macos-setup/go.mod
	$(MAKE) -C tools/macos-setup build

# Set to 1 to skip developertools.sh and macports.sh entirely (speeds up test
# builds when only configure.sh changes need to be validated).
SKIP_MACPORTS   ?= 0

# Initial password of the account provisioned in macos-27 (kept in the guest's ~/password),
# like the random one Lima generates for 15/26. Random per run unless set. If MACOS_PASSWORD
# is exported (e.g. from .envrc) every build sets it as the account password at the end.
ifndef PROVISION_PASSWORD
PROVISION_PASSWORD := $(shell openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)
endif
export PROVISION_PASSWORD

# ── Instance definitions ──────────────────────────────────────────────────────

INSTANCE_26      := macos-26
INSTANCE_27      := macos-27
INSTANCE_15      := macos-15
INSTANCE_14      := macos-14
INSTANCE_26_TEST := macos-26-test
INSTANCE_15_TEST := macos-15-test

CONFIG_26      := $(CURDIR)/macos-26.yaml
CONFIG_27      := $(CURDIR)/macos-27.yaml
CONFIG_15      := $(CURDIR)/macos-15.yaml
CONFIG_14      := $(CURDIR)/macos-14.yaml
CONFIG_26_TEST := $(CURDIR)/macos-26-test.yaml
CONFIG_15_TEST := $(CURDIR)/macos-15-test.yaml

RUNNER_26      := macOS_26
RUNNER_27      := macOS_27
RUNNER_15      := macOS_15
RUNNER_14      := macOS_14

.DEFAULT_GOAL := help

# Wait for the virtiofs mount (/Volumes/lima_mac) after a reboot.
# The Lima guest agent creates the symlink a few seconds after SSH is reachable.
# Usage: $(call wait_mount,INSTANCE_NAME)
define wait_mount
	@i=0; while ! $(LIMACTL) shell $(1) -- test -f /Volumes/lima_mac/configure.sh 2>/dev/null; do \
		i=$$((i+1)); \
		[ $$i -ge 24 ] && echo "[wait-mount] timed out waiting for virtiofs on $(1)" && exit 1; \
		echo "[wait-mount] not ready yet ($$i/24), retrying in 5s..."; \
		sleep 5; \
	done; echo "[wait-mount] virtiofs mount ready"
endef

# Sets MACOS_PASSWORD (if exported) as the account password and re-applies auto-login with it
# (configure.sh autologin -> configure_password, configure_autologin). No-op when unset.
# Usage: $(call set_password,INSTANCE_NAME)
define set_password
	@if [ -n "$$MACOS_PASSWORD" ]; then \
		$(LIMACTL) shell $(1) env MACOS_PASSWORD="$$MACOS_PASSWORD" /Volumes/lima_mac/configure.sh autologin; \
	else echo "[set-password] MACOS_PASSWORD not set; keeping the generated password (~/password)"; fi
endef

# Starts the GitHub runner on an existing VM (starting the VM first if needed). If the runner
# is not online, its registration is cleared and recreated via ghrunner, so a registration
# GitHub expired after inactivity is replaced. An online runner is left alone (no job cancels).
# Usage: $(call start_runner,INSTANCE_NAME,RUNNER_LABEL)
define start_runner
	@[ "$$($(LIMACTL) list $(1) --format '{{.Status}}')" = "Running" ] || $(LIMACTL) start $(1)
	@if [ "$$(gh api repos/$(GITHUB_OWNER)/$(GITHUB_REPO)/actions/runners --jq '.runners[] | select(.name=="$(2)") | .status')" = "online" ]; then \
		echo "[runner] $(2) is already online"; \
	else \
		$(GHRUNNER) -remove $(2) -owner $(GITHUB_OWNER) -repo $(GITHUB_REPO) && \
		$(GHRUNNER) -lima $(1) -owner $(GITHUB_OWNER) -repo $(GITHUB_REPO); \
	fi
endef

# Customizes a guest from the host once Lima has finished with it: stop the VM, edit its
# disk image (tools/macos-setup), start it again. Replaces the in-guest cliclick steps.
# Usage: $(call patch_vm,INSTANCE_NAME)
define patch_vm
	@[ "$$($(LIMACTL) list $(1) --format '{{.Status}}')" = "Stopped" ] || $(LIMACTL) stop $(1)
	$(MACOS_SETUP) -limactl $(LIMACTL) $(1)
	$(LIMACTL) start $(1)
	$(call wait_mount,$(1))
endef

# ── macOS 26 (release) ────────────────────────────────────────────────────────

build-26:
	$(LIMACTL) create --tty=false --name=$(INSTANCE_26) $(CONFIG_26)
	$(LIMACTL) start $(INSTANCE_26)
	$(LIMACTL) stop $(INSTANCE_26)
	$(LIMACTL) start $(INSTANCE_26)
	SKIP_OS_UPDATE=$(SKIP_OS_UPDATE) $(CURDIR)/os-update.sh $(INSTANCE_26) $(LIMACTL)
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_26) env XCODE_XIP=$(XCODE_XIP_26) /Volumes/lima_mac/developertools.sh
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_26) /Volumes/lima_mac/macports.sh
	$(CURDIR)/scripts/autologin-reboot.sh $(INSTANCE_26) $(LIMACTL)
	$(call wait_mount,$(INSTANCE_26))
	$(call set_password,$(INSTANCE_26))
	$(MAKE) patch-26
	$(LIMACTL) shell $(INSTANCE_26) env \
		RUNNER_LABEL=$(RUNNER_26) \
		RUNNER_TOKEN=$$(gh api repos/$(GITHUB_OWNER)/$(GITHUB_REPO)/actions/runners/registration-token --method POST --jq '.token') \
		/Volumes/lima_mac/configure.sh runner

patch-26: $(MACOS_SETUP)
	$(call patch_vm,$(INSTANCE_26))

clean-26:
	-$(GHRUNNER) -remove $(RUNNER_26)
	-$(LIMACTL) stop -f $(INSTANCE_26)
	$(LIMACTL) remove -f $(INSTANCE_26)

rebuild-26: clean-26 build-26

runner-26:
	$(call start_runner,$(INSTANCE_26),$(RUNNER_26))

# ── macOS 27 ──────────────────────────────────────────────────────────────────

# The account is created by Apple's native first-boot provisioning (see macos-27.yaml):
# The account is named after the host user (user.name in macos-27.yaml), full name from the host, home in
# /Users/<host user>. The initial password is random per run (PROVISION_PASSWORD)
# and is replaced by MACOS_PASSWORD (set_password) when that is exported.

build-27:
	@# limactl disables yq env operators (strenv), so expand in the shell and pass
	@# yq string literals, escaping backslashes and double quotes.
	q() { printf '"%s"' "$$(printf '%s' "$$1" | sed 's/[\\"]/\\&/g')"; }; \
	$(LIMACTL) create --tty=false --name=$(INSTANCE_27) \
		--set ".osOpts.Darwin.guestProvisioning.password = $$(q "$$PROVISION_PASSWORD")" \
		$(CONFIG_27)
	$(LIMACTL) start $(INSTANCE_27)
	$(LIMACTL) stop $(INSTANCE_27)
	$(LIMACTL) start $(INSTANCE_27)
	SKIP_OS_UPDATE=$(SKIP_OS_UPDATE) $(CURDIR)/os-update.sh $(INSTANCE_27) $(LIMACTL)
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_27) env XCODE_XIP=$(XCODE_XIP_27) /Volumes/lima_mac/developertools.sh
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_27) /Volumes/lima_mac/macports.sh
	$(CURDIR)/scripts/autologin-reboot.sh $(INSTANCE_27) $(LIMACTL)
	$(call wait_mount,$(INSTANCE_27))
	$(call set_password,$(INSTANCE_27))
	$(MAKE) patch-27
	$(LIMACTL) shell $(INSTANCE_27) env \
		RUNNER_LABEL=$(RUNNER_27) \
		RUNNER_TOKEN=$$(gh api repos/$(GITHUB_OWNER)/$(GITHUB_REPO)/actions/runners/registration-token --method POST --jq '.token') \
		/Volumes/lima_mac/configure.sh runner

patch-27: $(MACOS_SETUP)
	$(call patch_vm,$(INSTANCE_27))

clean-27:
	-$(GHRUNNER) -remove $(RUNNER_27)
	-$(LIMACTL) stop -f $(INSTANCE_27)
	$(LIMACTL) remove -f $(INSTANCE_27)

rebuild-27: clean-27 build-27

runner-27:
	$(call start_runner,$(INSTANCE_27),$(RUNNER_27))

# Dispatches the netatalk workflow on the macOS_27 runner only and waits for the result. Needs the runner
# registered by build-27.
ci-netatalk-27:
	@# The runner registered at the end of build-27 needs a moment to connect.
	@i=0; until [ "$$(gh api repos/$(GITHUB_OWNER)/$(GITHUB_REPO)/actions/runners --jq '.runners[] | select(.name=="$(RUNNER_27)") | .status')" = "online" ]; do \
		i=$$((i+1)); \
		[ $$i -ge 24 ] && { echo "runner $(RUNNER_27) is not online after 2 minutes; run 'make build-27' first"; exit 1; }; \
		sleep 5; \
	done
	@id=$$(gh workflow run build-netatalk.yml -R $(GITHUB_OWNER)/$(GITHUB_REPO) \
		-f run_macos27=true -f run_macos26=false -f run_macos15=false \
		| grep -o 'runs/[0-9]*' | cut -d/ -f2); \
	[ -n "$$id" ] || { echo "could not determine the dispatched run id"; exit 1; }; \
	echo "netatalk on $(RUNNER_27): https://github.com/$(GITHUB_OWNER)/$(GITHUB_REPO)/actions/runs/$$id"; \
	gh run watch $$id -R $(GITHUB_OWNER)/$(GITHUB_REPO) --exit-status

# ── macOS 15 (Sequoia) ────────────────────────────────────────────────────────

build-15:
	$(LIMACTL) create --tty=false --name=$(INSTANCE_15) $(CONFIG_15)
	$(LIMACTL) start $(INSTANCE_15)
	$(LIMACTL) stop $(INSTANCE_15)
	$(LIMACTL) start $(INSTANCE_15)
	SKIP_OS_UPDATE=$(SKIP_OS_UPDATE) $(CURDIR)/os-update.sh $(INSTANCE_15) $(LIMACTL)
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_15) env XCODE_XIP=$(XCODE_XIP_15) /Volumes/lima_mac/developertools.sh
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_15) /Volumes/lima_mac/macports.sh
	$(CURDIR)/scripts/autologin-reboot.sh $(INSTANCE_15) $(LIMACTL)
	$(call wait_mount,$(INSTANCE_15))
	$(call set_password,$(INSTANCE_15))
	$(MAKE) patch-15
	$(LIMACTL) shell $(INSTANCE_15) env \
		RUNNER_LABEL=$(RUNNER_15) \
		RUNNER_TOKEN=$$(gh api repos/$(GITHUB_OWNER)/$(GITHUB_REPO)/actions/runners/registration-token --method POST --jq '.token') \
		/Volumes/lima_mac/configure.sh runner

patch-15: $(MACOS_SETUP)
	$(call patch_vm,$(INSTANCE_15))

clean-15:
	-$(GHRUNNER) -remove $(RUNNER_15)
	-$(LIMACTL) stop -f $(INSTANCE_15)
	$(LIMACTL) remove -f $(INSTANCE_15)

rebuild-15: clean-15 build-15

runner-15:
	$(call start_runner,$(INSTANCE_15),$(RUNNER_15))

# ── macOS 14 (Sonoma) ────────────────────────────────────────────────────────

build-14:
	$(LIMACTL) create --tty=false --name=$(INSTANCE_14) $(CONFIG_14)
	$(LIMACTL) start $(INSTANCE_14)
	$(LIMACTL) stop $(INSTANCE_14)
	$(LIMACTL) start $(INSTANCE_14)
	SKIP_OS_UPDATE=$(SKIP_OS_UPDATE) $(CURDIR)/os-update.sh $(INSTANCE_14) $(LIMACTL)
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_14) env XCODE_XIP=$(XCODE_XIP_14) /Volumes/lima_mac/developertools.sh
	@[ "$(SKIP_MACPORTS)" = "1" ] || $(LIMACTL) shell $(INSTANCE_14) /Volumes/lima_mac/macports.sh
	$(CURDIR)/scripts/autologin-reboot.sh $(INSTANCE_14) $(LIMACTL)
	$(call wait_mount,$(INSTANCE_14))
	$(call set_password,$(INSTANCE_14))
	$(MAKE) patch-14
	$(LIMACTL) shell $(INSTANCE_14) env \
		RUNNER_LABEL=$(RUNNER_14) \
		RUNNER_TOKEN=$$(gh api repos/$(GITHUB_OWNER)/$(GITHUB_REPO)/actions/runners/registration-token --method POST --jq '.token') \
		/Volumes/lima_mac/configure.sh runner

patch-14: $(MACOS_SETUP)
	$(call patch_vm,$(INSTANCE_14))

clean-14:
	-$(GHRUNNER) -remove $(RUNNER_14)
	-$(LIMACTL) stop -f $(INSTANCE_14)
	$(LIMACTL) remove -f $(INSTANCE_14)

rebuild-14: clean-14 build-14

runner-14:
	$(call start_runner,$(INSTANCE_14),$(RUNNER_14))

# ── macOS 26 test (patch validation — no provisioning) ────────────────────────

build-26-test:
	$(LIMACTL) create --tty=false --name=$(INSTANCE_26_TEST) $(CONFIG_26_TEST)
	$(LIMACTL) start $(INSTANCE_26_TEST)

clean-26-test:
	-$(LIMACTL) stop -f $(INSTANCE_26_TEST)
	$(LIMACTL) remove -f $(INSTANCE_26_TEST)

rebuild-26-test: clean-26-test build-26-test

# ── macOS 15 test (patch validation — no provisioning) ────────────────────────

build-15-test:
	$(LIMACTL) create --tty=false --name=$(INSTANCE_15_TEST) $(CONFIG_15_TEST)
	$(LIMACTL) start $(INSTANCE_15_TEST)

clean-15-test:
	-$(LIMACTL) stop -f $(INSTANCE_15_TEST)
	$(LIMACTL) remove -f $(INSTANCE_15_TEST)

rebuild-15-test: clean-15-test build-15-test

# ── Status and help ───────────────────────────────────────────────────────────

status:
	$(LIMACTL) list

help:
	@echo "Lima macOS VM Management"
	@echo ""
	@echo "Usage: make [target] [VAR=value ...]"
	@echo ""
	@echo "Runner VMs (create, provision, install MacPorts, register GitHub runner):"
	@echo "  build-27           Build macOS 27 and register the macOS_27 runner"
	@echo "  clean-27           Deregister runner, stop, and remove macOS 27 VM"
	@echo "  rebuild-27         Clean then build macOS 27"
	@echo "  runner-27          Start the existing VM's macOS_27 runner; re-register it if offline"
	@echo ""
	@echo "  build-26           Build macOS 26 and register the macOS_26 runner"
	@echo "  clean-26           Deregister runner, stop, and remove macOS 26 VM"
	@echo "  rebuild-26         Clean then build macOS 26"
	@echo "  runner-26          Start the existing VM's macOS_26 runner; re-register it if offline"
	@echo ""
	@echo "  build-15           Build macOS 15 and register the macOS_15 runner"
	@echo "  clean-15           Deregister runner, stop, and remove macOS 15 VM"
	@echo "  rebuild-15         Clean then build macOS 15"
	@echo "  runner-15          Start the existing VM's macOS_15 runner; re-register it if offline"
	@echo ""
	@echo "  build-14           Build macOS 14 and register the macOS_14 runner"
	@echo "  clean-14           Deregister runner, stop, and remove macOS 14 VM"
	@echo "  rebuild-14         Clean then build macOS 14"
	@echo "  runner-14          Start the existing VM's macOS_14 runner; re-register it if offline"
	@echo ""
	@echo "Guest customization (host-side, tools/macos-setup):"
	@echo "  patch-<vm>         Stop a VM, customize its disk from the host (macos-setup), start it"
	@echo "                     (<vm>: 26, 27, 15, 14; run by build-<vm>, also usable alone)"
	@echo "  ci-netatalk-27     Run the netatalk workflow on the macOS_27 runner only and wait"
	@echo ""
	@echo "Test VMs (no runner registration):"
	@echo "  build-26-test      Create and start the stock macOS 26 patch-validation VM"
	@echo "  clean-26-test      Stop and remove the macOS 26 patch-validation VM"
	@echo "  rebuild-26-test    Clean then build the macOS 26 patch-validation VM"
	@echo ""
	@echo "  build-15-test      Create and start the stock macOS 15 patch-validation VM"
	@echo "  clean-15-test      Stop and remove the macOS 15 patch-validation VM"
	@echo "  rebuild-15-test    Clean then build the macOS 15 patch-validation VM"
	@echo ""
	@echo "Other:"
	@echo "  status             Show all Lima instance states"
	@echo "  help               Show this message"
	@echo ""
	@echo "Overridable variables (current value shown):"
	@echo "  LIMACTL=$(LIMACTL)"
	@echo "  LIMA_APP_BUNDLE=$(LIMA_APP_BUNDLE)"
	@echo "  GITHUB_OWNER=$(GITHUB_OWNER)"
	@echo "  GITHUB_REPO=$(GITHUB_REPO)"
	@echo "  XCODE_XIP_26=$(XCODE_XIP_26)  XCODE_XIP_27=$(XCODE_XIP_27)"
	@echo "  XCODE_XIP_15=$(XCODE_XIP_15)  XCODE_XIP_14=$(XCODE_XIP_14)   (Xcode .xip in lima_mac/xcode/; empty = CLT only)"
	@echo "  SKIP_OS_UPDATE=$(SKIP_OS_UPDATE)   (1 = skip the OS software update check)"
	@echo "  SKIP_MACPORTS=$(SKIP_MACPORTS)    (1 = skip developertools.sh and macports.sh)"
	@echo "  PROVISION_PASSWORD (build-27 initial account password; random per run if unset, kept in the guest's ~/password)"
