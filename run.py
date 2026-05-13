#!/usr/bin/env python3
"""
Standalone runner for the WEM Web Interface – no Home Assistant required.

Usage:
    python run.py [--debug] [--dump-html]
        python run.py --list-entries

  --debug      Enable DEBUG-level logging
  --dump-html  Save raw HTML of each fetched page to ./html_dumps/
    --list-entries  Recursively scan all menus and print only the discovered entries
                                    one per line, then exit.

Interactive commands (after discovery):
    list          – show all discovered parameters
    poll <stack>  – manually poll one stack entry
    set <name_fragment> <value>  – write a value
    range <entry-id>  – show valid values for select/number parameter
    rediscover <stack_index|stack_string>  – re-run discovery for one entry
    initscan [interval] [max_entries]  – one-time recursive stack initialization
    quit          – exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow importing from the custom_components package
sys.path.insert(0, str(Path(__file__).parent))

from custom_components.wem_webinterface.coordinator import WemCoordinator, _parse_entries
from custom_components.wem_webinterface.parser import parse_settings_page

# ---------------------------------------------------------------------------
# Default configuration – change here or via environment variables
# ---------------------------------------------------------------------------

DEFAULT_IP       = os.environ.get("WEM_IP",       "heizung.home")
DEFAULT_USER     = os.environ.get("WEM_USER",     "admin")
DEFAULT_PASSWORD = os.environ.get("WEM_PASSWORD", "C4v_mxfD43Lk")

DEFAULT_ENTRIES = [
    # Entry 1: writable numeric (Heizkreis 2 Raumsolltemperatur Komfort)
    "330000010000000000800070CF010002000301,"
    "330026000000000000800070CF020003000401,"
    "3300260100000000E6400070CF030011010401",
    # Entry 2: writable select (Systembetriebsart)
    "060000010000000000800070CF010011000301",
    # Entry 3: read-only multi-value (Wärmepumpe status + measurements)
    "0C0000010000000000800070CF010002000301,"
    "0C000C220000000000000070CF020003000401",
]


# ---------------------------------------------------------------------------
# Stub "hass" and "config_entry" objects for standalone use
# ---------------------------------------------------------------------------

class _FakeHass:
    """Minimal stub so the coordinator doesn't crash on hass=None."""

    async def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


class _FakeConfigEntry:
    domain = "wem_webinterface"
    entry_id = "standalone"
    data: Dict[str, Any] = {}
    options: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    logger = logging.getLogger("wem.run")

    dump_dir: Optional[Path] = None
    if args.dump_html:
        dump_dir = Path("html_dumps")
        dump_dir.mkdir(exist_ok=True)
        logger.info("HTML dumps will be saved to %s/", dump_dir)

    fake_entry = _FakeConfigEntry()

    coordinator = WemCoordinator(
        ip_address=DEFAULT_IP,
        username=DEFAULT_USER,
        password=DEFAULT_PASSWORD,
        entries=DEFAULT_ENTRIES,
        cycle_interval=args.cycle,
        retry_interval=args.retry,
        max_retries=args.retries,
        hass=_FakeHass(),
        config_entry=fake_entry,
    )

    # Patch to enable HTML dumping if requested
    if dump_dir:
        _patch_for_html_dump(coordinator, dump_dir)

    if not args.list_entries:
        # Register a simple callback to print value changes
        coordinator.register_new_param_callback(_on_new_param)

    logger.info("Connecting to http://%s …", DEFAULT_IP)

    try:
        await coordinator.async_setup()
    except Exception as exc:
        logger.error("Setup failed: %s (%r)", exc.__class__.__name__, exc)
        await coordinator.async_teardown()
        return

    if args.list_entries:
        logger.info("Running recursive menu scan and printing entries only …")
        try:
            await coordinator.async_initialize_entries(
                scan_interval_seconds=args.cycle,
                max_entries=500,
            )
        except Exception as exc:
            logger.error("Entry scan failed: %s (%r)", exc.__class__.__name__, exc)
            await coordinator.async_teardown()
            return

        _print_entries(coordinator)
        logger.info("Printed %d entry(ies).", len(coordinator.entries))
        logger.info("Shutting down…")
        await coordinator.async_teardown()
        return

    _print_all(coordinator)

    if args.interactive:
        await _interactive_loop(coordinator, logger)
    else:
        logger.info(
            "Polling loop running (Ctrl-C to stop). Cycle interval: %ds",
            coordinator.cycle_interval,
        )
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass

    logger.info("Shutting down…")
    await coordinator.async_teardown()


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

async def _on_new_param(stack, params):
    print(f"\n[Discovery]  Stack: {stack[:50]}")
    for p in params:
        extra = ""
        if p.param_type == "number":
            extra = f"  range=[{p.min_value}…{p.max_value} step {p.step}]  unit={p.unit}"
        elif p.param_type == "select":
            extra = f"  options={p.options}"
        elif p.unit:
            extra = f"  unit={p.unit}"
        print(f"  [{p.param_type:8s}]  {p.name}  =  {p.current_value}{extra}")


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

async def _interactive_loop(coordinator: WemCoordinator, logger) -> None:
    print("\nInteractive mode. Type 'help' for commands.")
    loop = asyncio.get_event_loop()
    while True:
        try:
            raw = await loop.run_in_executor(None, input, "\nwem> ")
        except (EOFError, KeyboardInterrupt):
            break
        parts = raw.strip().split(None, 2)
        if not parts:
            continue
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break

        elif cmd == "help":
            print(
                "  list                          – show all parameters\n"
                "  poll <idx|stack>              – poll one stack\n"
                "  set <name_fragment> <value>   – write a value\n"
                "  range <entry-id>              – show valid values/options\n"
                "  rediscover <idx|stack>        – re-discover one stack\n"
                "  initscan [interval] [max]     – one-time recursive initialization scan\n"
                "  quit                          – exit"
            )

        elif cmd == "list":
            _print_all(coordinator)

        elif cmd == "poll":
            if len(parts) < 2:
                print("Usage: poll <index|stack_string>")
                continue
            stack = _resolve_stack(coordinator, parts[1])
            if stack:
                await coordinator._poll_stack(stack)
                _print_stack(coordinator, stack)
            else:
                print(f"Unknown stack: {parts[1]}")

        elif cmd == "set":
            if len(parts) < 3:
                print("Usage: set <name_fragment> <value>")
                continue
            frag, val = parts[1], parts[2]
            match = _find_param(coordinator, frag)
            if match is None:
                print(f"No parameter matching '{frag}'")
            else:
                stack, param_id, info = match
                print(f"Setting '{info.name}' = {val}")
                await coordinator.request_write(stack, param_id, val)

        elif cmd == "range":
            if len(parts) < 2:
                print("Usage: range <entry-id>")
                continue
            info = _resolve_param_info(coordinator, parts[1])
            if info is None:
                print(f"No parameter found for '{parts[1]}'")
                continue
            _print_parameter_range(info)

        elif cmd == "rediscover":
            if len(parts) < 2:
                print("Usage: rediscover <index|stack_string>")
                continue
            stack = _resolve_stack(coordinator, parts[1])
            if stack:
                await coordinator.async_rediscover_stack(stack)
                _print_stack(coordinator, stack)
            else:
                print(f"Unknown stack: {parts[1]}")

        elif cmd == "initscan":
            interval = 10
            max_entries = 500
            if len(parts) >= 2:
                try:
                    interval = int(parts[1])
                except ValueError:
                    print("Usage: initscan [interval_seconds>=10] [max_entries]")
                    continue
            if len(parts) == 3:
                try:
                    max_entries = int(parts[2])
                except ValueError:
                    print("Usage: initscan [interval_seconds>=10] [max_entries]")
                    continue

            if interval < 10:
                print("Interval must be at least 10 seconds.")
                continue

            print(
                f"Starting initialization scan (interval={interval}s, max_entries={max_entries}) ..."
            )
            result = await coordinator.async_initialize_entries(
                scan_interval_seconds=interval,
                max_entries=max_entries,
            )
            print(
                "Initialization scan finished: "
                f"processed={result['processed']} new_entries={result['new_entries']} "
                f"failed={result['failed']} total_entries={result['total_entries']}"
            )

        else:
            print(f"Unknown command: {cmd}")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_all(coordinator: WemCoordinator) -> None:
    params = coordinator.get_all_parameters()
    if not params:
        print("No parameters discovered yet.")
        return
    print(f"\n{'Index':<5} {'Type':<9} {'Name':<55} {'Value':<15} {'Unit'}")
    print("-" * 110)
    for i, p in enumerate(params):
        print(f"{i:<5} {p.param_type:<9} {p.name[:54]:<55} {str(p.current_value):<15} {p.unit}")


def _print_entries(coordinator: WemCoordinator) -> None:
    seen: set[str] = set()
    for entry in coordinator.entries:
        if entry in seen:
            continue
        seen.add(entry)
        print(entry)


def _print_stack(coordinator: WemCoordinator, stack: str) -> None:
    params = [p for p in coordinator.get_all_parameters() if p.stack == stack]
    for p in params:
        print(f"  {p.name}  =  {p.current_value} {p.unit}")


def _resolve_stack(coordinator: WemCoordinator, ref: str) -> Optional[str]:
    if ref.isdigit():
        idx = int(ref)
        if 0 <= idx < len(coordinator.entries):
            return coordinator.entries[idx]
    if ref in coordinator.entries:
        return ref
    # Partial match
    for e in coordinator.entries:
        if ref in e:
            return e
    return None


def _find_param(coordinator: WemCoordinator, fragment: str):
    fragment_lower = fragment.lower()
    for p in coordinator.get_all_parameters():
        if fragment_lower in p.name.lower() or fragment_lower in p.param_id.lower():
            return p.stack, p.param_id, p
    return None


def _resolve_param_info(coordinator: WemCoordinator, entry_id: str):
    """Resolve a parameter by list index, exact param_id or name fragment."""
    params = coordinator.get_all_parameters()
    if entry_id.isdigit():
        idx = int(entry_id)
        if 0 <= idx < len(params):
            return params[idx]

    for p in params:
        if entry_id == p.param_id:
            return p

    entry_lower = entry_id.lower()
    for p in params:
        if entry_lower in p.name.lower() or entry_lower in p.param_id.lower():
            return p
    return None


def _print_parameter_range(info) -> None:
    """Print valid values/options for one parameter."""
    print(f"{info.name} [{info.param_type}]")

    if info.param_type == "select":
        options = list(info.options or [])
        if not options:
            print("  No options available.")
            return
        print("  Allowed values:")
        for opt in options:
            print(f"    - {opt}")
        return

    if info.param_type == "number":
        min_v = info.min_value
        max_v = info.max_value
        step = info.step
        unit = f" {info.unit}" if info.unit else ""
        print(f"  Range: {min_v} .. {max_v} (step {step}){unit}")

        if min_v is None or max_v is None or step in (None, 0):
            print("  No complete numeric range metadata available.")
            return

        values: List[float] = []
        cur = float(min_v)
        # Include max value with a small tolerance for float rounding.
        while cur <= float(max_v) + 1e-9 and len(values) < 500:
            values.append(round(cur, 10))
            cur += float(step)

        if not values:
            print("  Could not derive concrete values from metadata.")
            return

        if len(values) <= 50:
            print("  Allowed values:")
            print("   ", ", ".join(str(v) for v in values))
        else:
            preview = ", ".join(str(v) for v in values[:10])
            tail = ", ".join(str(v) for v in values[-5:])
            print(f"  Total values: {len(values)}")
            print(f"  First values: {preview} ...")
            print(f"  Last values: ... {tail}")
        return

    print("  This parameter is read-only and has no writable range/options.")


# ---------------------------------------------------------------------------
# HTML dump patch
# ---------------------------------------------------------------------------

def _patch_for_html_dump(coordinator: WemCoordinator, dump_dir: Path) -> None:
    """Monkey-patch _fetch_stack to also save raw HTML."""
    original = coordinator._fetch_stack

    async def patched(stack: str):
        result = await original(stack)
        # Re-fetch just to get the raw HTML for dumping (we accept the extra request)
        # Actually, we patch lower-level to capture HTML. For simplicity, just note the result.
        slug = stack[:30].replace(",", "_")
        # Save a file with the parsed result
        out = dump_dir / f"{slug}.txt"
        if result is not None:
            out.write_text(
                "\n".join(
                    f"{p.name} [{p.param_type}] = {p.current_value} {p.unit}" for p in result
                ),
                encoding="utf-8",
            )
        return result

    coordinator._fetch_stack = patched


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WEM Web Interface standalone runner")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--dump-html", action="store_true", help="Save HTML dumps to ./html_dumps/")
    parser.add_argument(
        "--list-entries",
        action="store_true",
        help="Recursively scan all menus and print only the discovered entries, one per line",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Start interactive REPL")
    parser.add_argument("--cycle", type=int, default=20, help="Cycle interval in seconds (default 20)")
    parser.add_argument("--retry", type=int, default=5, help="Retry interval in seconds (default 5)")
    parser.add_argument("--retries", type=int, default=3, help="Max retries on incomplete page (default 3)")
    args = parser.parse_args()

    asyncio.run(main(args))
