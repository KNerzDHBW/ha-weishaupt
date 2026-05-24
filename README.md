# WEM Web Interface (Home Assistant), v0.2.2

Custom integration for Home Assistant to read and write values from the *local* WEM webserver.

Current integration version: 0.2.2

## Warning

This project documentation and code were created with GitHub Copilot (GPT-5.3-Codex). I have no clue what it did, but it is working on my machine.
No guarantee is provided for correctness, completeness, or safety.

Use this integration at your own risk.
It could accidentially set heating parameters to wrong operating values and may cause malfunctions or physical damage to the heating system.
This could happen without explicitly setting it, just by a bug of the Copilot Code.

## Operational Limitations

- Reading and writing values is not done in real-time. Depending on polling interval, request spacing, and verification retries, applying or updating a new value can take minutes.
- The Weishaupt web interface is highly unstable (at least for some machines). Intermittent page load failures, incomplete pages, and temporary login failures are expected. Parallel access often leads to incorrect responses which easily can lead to wrong values been set.
- To reduce errors, the integration performs updates with pauses between requests and retries failed operations.
- Do not use the Weishaupt web interface in parallel (browser) while Home Assistant is reading or writing values. Concurrent usage can invalidate sessions and cause wrong reads, failed writes, or stale values.
- Setup username/passwort via the Webinterface (see below) before starting the Addon for the first time.


## Activating the Webserver

Before adding this integration in Home Assistant, you must enable the webserver on the heater controller:

1. Open the heater settings on the device.
2. Switch to OEM mode (code `21`).
   **You do this on your own risk! This might break your heater!**
3. Enable the Webserver in `Settings` -> `Webserver`.
4. Enable it (not service!).
5. Login to the webinterface - ip address of your heater (can be seen in `Settings` -> `TCP/IP`) - and set username and password.

**Warning:**

- This is done at your own risk.
- Enabling this interface allows configuration changes that can modify safety-relevant heating behavior.
- Incorrect changes can cause malfunctions or physical damage to the heating system.

**Note:**
At least for some devices, this webserver is **really** unstable.
* It crashes sometimes if it got too many accesses.
* It answeres sometimes with only parts of the real page or with just the wrong page.
* It cannot handle simultianious accesses
* ...

Use it with real care!

## Installing the AddOn in HACS

If you do not have HACS in Home Assistant, then install it.
If you cannot find how, you should not use this AddOn. (Sorry for being blunt)

1. In Home Assistant, open HACS.
2. Go to `Custom repositories`.
3. Add `https://github.com/KNerzDHBW/ha-weishaupt` and choose category `Integration`.
4. Install `WEM Web Interface` from HACS.
5. Restart Home Assistant.

## Configuration

**Warning:**
The configuration can easily take ten minutes with manual steps in between!

### Start Integration Configuration
Add the integration WEM Web Interface in `Settings` -> `Devices & Services`.
Insert

- IP address or DNS name (e.g. `heizung.home`)
- Username
- Password
- Minimum request pause (see instability of webserver)

### Read All Main-Menu Entries

Now, the AddOn will try to connect to the heater and read the main menu of all entries to be read.
In the next window, the currently processed step can be seen.
As I couldn't figure out, how to automatically update the log windows (Copilot...), so you have to update the window by clicking "Refresh Log Data".
You need to do this in order to come to the next step (sorry).

This can take one or two minutes!

It might result in an error (incorrect username/password) even if the data is correct (Webinterface is unstable).
Just retry. If it fails too often, make sure url, username, and password are correct and try a higher minimum request pause.

### Choose active menu entries

The next step is to choose the main menu entries to be used.
As the refresh rate depends on the amount of entries to be read and writing is unstable, disable all menu items, you do not want to read/write.

*Disable at least `Inputs`, `Outputs`, and `Settings` as changing them could be really harmful to your heating system.*

Afterwards the AddOn will try to process each menu entry, read all sub-menu entries. This takes quite some time (in particular with long request pause), as each sub-menu entry has to be processed separately.

Again you can "Refresh Log Data" to see what is happening. You'll see a list of all entries which are found.

**Warning:** If you see corrupt information, cancel and restart (again, instabile webserver).

After all sub-menu items are read, the AddOn will read the information for each writable entries which again has to open each writeable entry.
This will be shown as "Finalizing writable items".
And again, this takes times depending on the pause time.

Again: You have to refresh the log data to come to the next step.
When it is done, you can click "Finish"

### Finalization

Home Assistant persists the results, creates entities etc.
This can take a minute (without showing any log!) as each entry has additional information about the url where to find it (which by the way changes for some reason).

### Final Touches

Go into the newly created integration and **disable all writeable entries which could harm your heater, all you do not understand, all not supported by the plugin** (e.g. times and dates or value is "unkown"), and all you are not interested in.

This does **not** prevent that bugs in this AddOn or the Webserver overwrite these values, but it makes it less likely.

Go to the settings of the Integration and **disable all sub-menu entries which could harm your heater, all you do not understand, all not supported by the plugin** (e.g. times and dates or value is "unkown"), and all you are not interested in.

## Duplicate Entities Cleanup

If stack/URL IDs change on the WEM web interface, old entities can remain in Home Assistant as unavailable duplicates.

The integration includes a maintenance service to clean them up:

- Service: `wem_webinterface.cleanup_duplicates`
- Effect: keeps the best matching entity per logical point and removes duplicate registry entries.

Recommended usage:

1. Reload the integration (or restart Home Assistant) so the service is registered.
2. Open Developer Tools -> Services.
3. Run `wem_webinterface.cleanup_duplicates`.


## Further Restrictions

* Dates, Times, and other entries are not supported.
* Between setting values a short waiting time should be considered. For exmple waiting two seconds between setting two values.

## Notes

- `run.py` and `_inspect_wem.py` are development/testing helpers and are not used by Home Assistant runtime.
- Update `documentation` and `issue_tracker` URLs in `custom_components/wem_webinterface/manifest.json` to your real repository URL.
