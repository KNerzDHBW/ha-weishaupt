# NewTry - WEM standalone init + poll

Start script:

```powershell
.\.venv\Scripts\python.exe .\NewTry\wem_init_poll.py
```

Optional wait-time override:

```powershell
.\.venv\Scripts\python.exe .\NewTry\wem_init_poll.py --wait-seconds 5
```

What this script does:

1. Logs in to `http://heizung.home` with configured credentials.
2. Opens `settings_export.html`.
3. Traverses menu links two levels deep.
4. Reads sub entries and values on level-2 pages.
5. On first run only, opens editor links (if present) to read range/options.
6. Polls continuously by re-reading level-2 pages only.
7. Prints every read value to console.

Constraints implemented:

- Every request waits for at least 3 seconds since the previous request by default.
- The wait time is configurable via `--wait-seconds`.
- Each request uses retries because pages can fail or load incompletely.

Configured in script constants:

- `BASE_URL`
- `USERNAME`
- `PASSWORD`
- `MIN_REQUEST_GAP_SECONDS` (default for `--wait-seconds`)
- `MAX_RETRIES`
