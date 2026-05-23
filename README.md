# WEM Web Interface (Home Assistant), v0.1.14

Custom integration for Home Assistant to read and write values from the local WEM web interface.

Current integration version: 

## HACS Installation

1. In Home Assistant, open HACS.
2. Go to `Custom repositories`.
3. Add your repository URL and choose category `Integration`.
4. Install `WEM Web Interface` from HACS.
5. Restart Home Assistant.
6. Add the integration in `Settings -> Devices & Services`.

## Configuration

Configure in the integration UI:

- IP address or DNS name (e.g. `heizung.home`)
- Username
- Password
- Stack entries (one stack per line) [OPTIONAL]
- In the configuration of the wem web interface read all entries.

## Initialization During Setup

During first-time setup, the integration runs a multi-step initialization flow. The steps run in sequence:

1. Establish connection and verify login.
2. Read available main menus.
3. Recursively scan selected main menus and submenus.
4. Collect and classify discovered points.
5. `Finalizing writable items: x/y`: inspect writable entries one by one (type, min/max/step, options, and write metadata).
6. Persist results and create entities in Home Assistant.

Important timing note:

- While initializing the first time, a dialog is kept open. Pressing the button will update the log information (no idea how to do that correctly). As soon as the reading is done, the button will step to the next step.
- The last step takes a minute without showing anything but an empty page. Sorry about that.
- Full initialization can take several minutes (depending on the number of menus/submenus and request spacing).
- During this time, the UI may continue to show "Initializing".
- This is expected behavior; in most cases the run should not be interrupted.

Note:

- The status window shows newest messages first (`Latest first (newest at top):`).

## Why It Continues After Finish

After clicking `Finish`, Home Assistant still performs the normal config entry setup lifecycle.

This is expected and consists of two distinct phases:

1. Config flow phase: the dialog collects credentials/options and performs the interactive initialization scan.
2. Entry setup phase: Home Assistant loads the integration, forwards platforms, restores cached/bootstrapped data, and makes entities available.

So even if the scan in the dialog already finished, there can still be a short "Initializing" period while Home Assistant activates the entry.

Performance note:

- The integration now passes bootstrap scan data from the config flow into entry setup whenever possible.
- This avoids a second full discovery in most cases and reduces the post-Finish waiting time.

## Duplicate Entities Cleanup

If stack/URL IDs change on the WEM web interface, old entities can remain in Home Assistant as unavailable duplicates.

The integration includes a maintenance service to clean them up:

- Service: `wem_webinterface.cleanup_duplicates`
- Effect: keeps the best matching entity per logical point and removes duplicate registry entries.

Recommended usage:

1. Reload the integration (or restart Home Assistant) so the service is registered.
2. Open Developer Tools -> Services.
3. Run `wem_webinterface.cleanup_duplicates`.

Note:

- New duplicate creation is prevented by stable logical matching in the integration.
- This service is intended to remove already existing duplicates from the entity registry.

## Notes

- `run.py` and `_inspect_wem.py` are development/testing helpers and are not used by Home Assistant runtime.
- Update `documentation` and `issue_tracker` URLs in `custom_components/wem_webinterface/manifest.json` to your real repository URL.
