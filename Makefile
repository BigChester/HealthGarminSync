PYTHON ?= .venv/bin/python
# Explicit public paths avoid traversing local credentials, state, or AGENTS.md.
MARKDOWN = README.md CONTRIBUTING.md CHANGELOG.md \
	docs/SHORTCUTS.md docs/VERIFICATION.md docs/OPERATIONS.md \
	docs/ARCHITECTURE.md docs/SOURCES.md

BRIDGE_BUILD_PATH ?= .build/health-bridge
BRIDGE_SWIFT_FLAGS ?=

.PHONY: setup dev format check test format-bridge check-bridge test-bridge build-ios

setup:
	bash scripts/setup.sh

dev:
	$(PYTHON) -m pip install -r requirements.txt -r requirements-dev.txt

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .
	$(PYTHON) -m mdformat $(MARKDOWN)

check:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m mdformat --check $(MARKDOWN)
	bash -n scripts/setup.sh health-garmin-sync
	git diff --check

test:
	$(PYTHON) -c 'from importlib.metadata import version; import garminconnect, requests, urllib3, curl_cffi; assert version("garminconnect") == "0.3.16", "Install the reviewed Garmin SDK before running tests"'
	$(PYTHON) -m unittest discover -s tests -v

format-bridge:
	swift format format --in-place --recursive ios/GarminHealthBridge

check-bridge:
	swift format lint --strict --recursive ios/GarminHealthBridge
	plutil -lint ios/GarminHealthBridge/App/Info.plist ios/GarminHealthBridge/App/GarminHealthBridge.entitlements ios/GarminHealthBridge/App/GarminHealthBridge.xcodeproj/project.pbxproj

test-bridge:
	swift test --package-path ios/GarminHealthBridge --scratch-path $(BRIDGE_BUILD_PATH) --disable-xctest $(BRIDGE_SWIFT_FLAGS)
	swift build --package-path ios/GarminHealthBridge --scratch-path $(BRIDGE_BUILD_PATH) --product BridgeFixtureDriver $(BRIDGE_SWIFT_FLAGS)
	$(PYTHON) scripts/test_health_bridge.py --driver "$$(swift build --package-path ios/GarminHealthBridge --scratch-path $(BRIDGE_BUILD_PATH) --show-bin-path $(BRIDGE_SWIFT_FLAGS))/BridgeFixtureDriver"

build-ios:
	xcodebuild -project ios/GarminHealthBridge/App/GarminHealthBridge.xcodeproj -scheme GarminHealthBridge -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath .build/ios CODE_SIGNING_ALLOWED=NO build
