PLIST_SRC  := $(shell pwd)/deploy/com.juggle.watchdog.plist
PLIST_DST  := $(HOME)/Library/LaunchAgents/com.juggle.watchdog.plist
LABEL      := com.juggle.watchdog

.PHONY: watchdog-install watchdog-uninstall

watchdog-install:
	@echo "Installing juggle watchdog launchd service..."
	cp "$(PLIST_SRC)" "$(PLIST_DST)"
	launchctl bootstrap gui/$(shell id -u) "$(PLIST_DST)" 2>/dev/null || \
	  launchctl load -w "$(PLIST_DST)"
	@echo "Watchdog installed. Check status: launchctl print gui/$(shell id -u)/$(LABEL)"

watchdog-uninstall:
	@echo "Removing juggle watchdog launchd service..."
	launchctl bootout gui/$(shell id -u)/$(LABEL) 2>/dev/null || \
	  launchctl unload -w "$(PLIST_DST)" 2>/dev/null || true
	rm -f "$(PLIST_DST)"
	@echo "Watchdog uninstalled."
