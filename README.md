# WEM Web Interface (Home Assistant), v0.1.7

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

## Notes

- `run.py` and `_inspect_wem.py` are development/testing helpers and are not used by Home Assistant runtime.
- Update `documentation` and `issue_tracker` URLs in `custom_components/wem_webinterface/manifest.json` to your real repository URL.
