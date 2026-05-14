"""
WEM Web Interface – Coordinator

Handles:
  - Session management (login + cookie reuse)
  - Cyclic polling of each stack entry (one entry per cycle)
  - Discovery of parameter metadata on first run (and on demand)
  - Write queue: set a value, verify (up to 3×), then resume cycle
  - Retry on incomplete page loads (up to max_retries × retry_interval)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import platform
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    CONF_CYCLE_INTERVAL,
    CONF_IP_ADDRESS,
    CONF_MAX_RETRIES,
    CONF_PASSWORD,
    CONF_REDISCOVER_STACK,
    CONF_RETRY_INTERVAL,
    CONF_USERNAME,
    CONF_ENTRIES,
    DEFAULT_CYCLE_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_WRITE_RETRIES,
    DEFAULT_INITIAL_ENTRIES,
    DEFAULT_RETRY_INTERVAL,
)
from .parser import ParsedParameter, is_login_page, parse_settings_page

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParameterInfo:
    """Full metadata + current state for one discovered parameter."""
    stack: str                              # comma-separated stack string
    param_id: str                           # slugified name, unique within stack
    name: str
    param_type: str                         # "number" | "select" | "readonly"
    unit: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    step: Optional[float] = None
    options: Optional[List[str]] = None
    form_field_name: Optional[str] = None  # HTML field name for POST writes
    write_action: Optional[str] = None     # relative POST endpoint (e.g. pro_save.html)
    write_fields: Optional[Dict[str, str]] = None  # hidden form fields for writes
    current_value: Any = None
    last_updated: Optional[datetime] = None
    discovery_failed: bool = False


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class WemCoordinator:
    """Core coordinator – can be used standalone (run.py) or inside HA."""

    def __init__(
        self,
        ip_address: str,
        username: str,
        password: str,
        entries: List[str],
        cycle_interval: int = DEFAULT_CYCLE_INTERVAL,
        retry_interval: int = DEFAULT_RETRY_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        hass=None,
        config_entry=None,
    ):
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.entries = entries                    # List of stack strings
        self.cycle_interval = cycle_interval
        self.retry_interval = retry_interval
        self.max_retries = max_retries
        self.hass = hass
        self.config_entry = config_entry

        self._session: Optional[aiohttp.ClientSession] = None
        self._parameters: Dict[str, ParameterInfo] = {}   # key → ParameterInfo
        self._callbacks: Dict[str, List[Callable]] = {}    # key → [callback, ...]
        self._new_param_callbacks: List[Callable] = []     # called after discovery
        self._status_callbacks: List[Callable] = []
        self._last_successful_read: Optional[datetime] = None

        self._write_event = asyncio.Event()
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._poll_task: Optional[asyncio.Task] = None
        self._rediscover_retry_task: Optional[asyncio.Task] = None
        self._current_index: int = 0
        self._running: bool = False
        self._missing_values_retry: Dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Factory from HA ConfigEntry
    # ------------------------------------------------------------------

    @classmethod
    def from_config_entry(cls, hass, config_entry) -> "WemCoordinator":
        data = config_entry.data
        opts = config_entry.options
        entries_raw = opts.get(CONF_ENTRIES, data.get(CONF_ENTRIES, ""))
        entries = _parse_entries(entries_raw)
        return cls(
            ip_address=data[CONF_IP_ADDRESS],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            entries=entries,
            cycle_interval=int(opts.get(CONF_CYCLE_INTERVAL, DEFAULT_CYCLE_INTERVAL)),
            retry_interval=int(opts.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL)),
            max_retries=int(opts.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES)),
            hass=hass,
            config_entry=config_entry,
        )

    # ------------------------------------------------------------------
    # Properties / accessors
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.ip_address}"

    def make_key(self, stack: str, param_id: str) -> str:
        return f"{stack}::{param_id}"

    def get_parameter(self, stack: str, param_id: str) -> Optional[ParameterInfo]:
        return self._parameters.get(self.make_key(stack, param_id))

    def get_all_parameters(self) -> List[ParameterInfo]:
        return list(self._parameters.values())

    @property
    def last_successful_read(self) -> Optional[datetime]:
        return self._last_successful_read

    @staticmethod
    def _has_usable_value(value: Any) -> bool:
        """Return True if a parsed value should replace the current entity state."""
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        return True

    # ------------------------------------------------------------------
    # Callback registration (used by HA entities)
    # ------------------------------------------------------------------

    def register_update_callback(self, stack: str, param_id: str, cb: Callable) -> None:
        key = self.make_key(stack, param_id)
        self._callbacks.setdefault(key, []).append(cb)

    def unregister_update_callback(self, stack: str, param_id: str, cb: Callable) -> None:
        key = self.make_key(stack, param_id)
        try:
            self._callbacks.get(key, []).remove(cb)
        except ValueError:
            pass

    def register_new_param_callback(self, cb: Callable) -> None:
        """Register a callback invoked with (stack, List[ParsedParameter]) after discovery."""
        self._new_param_callbacks.append(cb)

    def register_status_callback(self, cb: Callable) -> None:
        """Register a callback invoked when coordinator-level diagnostics change."""
        self._status_callbacks.append(cb)

    def unregister_status_callback(self, cb: Callable) -> None:
        try:
            self._status_callbacks.remove(cb)
        except ValueError:
            pass

    async def _mark_successful_read(self, when: Optional[datetime] = None) -> None:
        """Track the last time any parameter was read successfully."""
        self._last_successful_read = when or datetime.now()
        for cb in list(self._status_callbacks):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb()
                else:
                    cb()
            except Exception as exc:
                _LOGGER.error("Error in status callback: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Login, discover all stacks, then start background polling."""
        _LOGGER.info(
            "Coordinator setup started for host=%s (entries=%d, cycle=%ss, retry=%ss, max_retries=%d)",
            self.ip_address,
            len(self.entries),
            self.cycle_interval,
            self.retry_interval,
            self.max_retries,
        )
        await self._create_session()
        try:
            await self._check_ip_reachability()
            _LOGGER.info("Reachability check (DNS/ping) passed for host=%s", self.ip_address)
            await self._check_web_port_reachability()
            _LOGGER.info("Port 80 check passed for host=%s", self.ip_address)
            await self._login()
            _LOGGER.info("Login completed for host=%s", self.ip_address)

            if not self.entries:
                added = await self._bootstrap_entries_from_home()
                if added == 0:
                    self.entries = list(DEFAULT_INITIAL_ENTRIES)
                    _LOGGER.warning(
                        "Bootstrap found no menu links for host=%s; falling back to default initial entries (%d stacks)",
                        self.ip_address,
                        len(self.entries),
                    )
                    added = len(self.entries)
                _LOGGER.info(
                    "Initial bootstrap after login finished for host=%s: added=%d total_entries=%d",
                    self.ip_address,
                    added,
                    len(self.entries),
                )

            await self._discover_all()
            self._running = True
            self._poll_task = asyncio.ensure_future(self._polling_loop())
            self._start_selected_rediscover_retry()
            _LOGGER.info("Coordinator setup finished for host=%s", self.ip_address)
        except Exception:
            _LOGGER.exception("Coordinator setup failed for host=%s", self.ip_address)
            if self._session and not self._session.closed:
                await self._session.close()
            raise

    async def async_teardown(self) -> None:
        """Stop polling and close HTTP session."""
        self._running = False
        self._write_event.set()          # unblock any waiting sleep
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._rediscover_retry_task:
            self._rediscover_retry_task.cancel()
            try:
                await self._rediscover_retry_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Session / login
    # ------------------------------------------------------------------

    async def _create_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        self._session = aiohttp.ClientSession(connector=connector, cookie_jar=jar)

    async def _check_ip_reachability(self, timeout_seconds: int = 5) -> None:
        """Check whether the configured host is reachable before port checks.

        For literal IP addresses this performs a single ping.
        For DNS hostnames this validates name resolution.
        """
        target = self.ip_address

        try:
            ipaddress.ip_address(target)
            is_literal_ip = True
        except ValueError:
            is_literal_ip = False

        if not is_literal_ip:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(socket.getaddrinfo, target, None),
                    timeout=timeout_seconds,
                )
                return
            except asyncio.TimeoutError as exc:
                raise ConnectionError(
                    f"WEM host {target} DNS lookup timed out after {timeout_seconds}s."
                ) from exc
            except socket.gaierror as exc:
                raise ConnectionError(
                    f"WEM host {target} could not be resolved via DNS ({exc})."
                ) from exc

        system = platform.system().lower()
        if system.startswith("win"):
            command = ["ping", "-n", "1", "-w", str(timeout_seconds * 1000), target]
        else:
            command = ["ping", "-c", "1", "-W", str(timeout_seconds), target]

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds + 1,
                ),
                timeout=timeout_seconds + 3,
            )
        except FileNotFoundError as exc:
            raise ConnectionError(
                "Ping command not available on this system. Cannot check host reachability first."
            ) from exc
        except (subprocess.TimeoutExpired, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"WEM device {target} did not answer ping within {timeout_seconds}s."
            ) from exc

        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            detail = f": {stderr}" if stderr else ""
            raise ConnectionError(
                f"WEM device {target} is not reachable on the network (ping failed){detail}"
            )

    async def _check_web_port_reachability(self, timeout_seconds: int = 5) -> None:
        """Check whether the web interface is reachable on port 80."""
        try:
            connect = asyncio.open_connection(self.ip_address, 80)
            reader, writer = await asyncio.wait_for(connect, timeout=timeout_seconds)
            writer.close()
            if hasattr(writer, "wait_closed"):
                await writer.wait_closed()
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                f"WEM device {self.ip_address} did not answer on port 80 within {timeout_seconds}s."
            ) from exc
        except (ConnectionRefusedError, OSError, socket.gaierror) as exc:
            raise ConnectionError(
                f"WEM device {self.ip_address} is not reachable on port 80. Network or firewall problem: {exc.__class__.__name__}."
            ) from exc
        except Exception as exc:
            raise ConnectionError(
                f"WEM device {self.ip_address} is not reachable on port 80. Unexpected error: {exc.__class__.__name__}."
            ) from exc

    async def _check_reachability(self, timeout_seconds: int = 5) -> None:
        """Backward-compatible wrapper for the two-step reachability check."""
        await self._check_ip_reachability(timeout_seconds=timeout_seconds)
        await self._check_web_port_reachability(timeout_seconds=timeout_seconds)

    async def _login(self) -> None:
        """Perform form-based login and persist the session cookie.

        Strategy: POST user/pass to /login.html and trust the cookie the
        device sets.  Whether the credentials are correct is validated lazily
        by _fetch_stack_html (which will keep receiving the login page and
        eventually give up if the password is wrong).  Doing an explicit probe
        here is unreliable because many WEM firmware versions redirect
        *every* page – including protected ones – to /login.html in ways
        that defeat simple heuristics.
        """
        # GET the base URL first so the session has any pre-login cookies.
        try:
            async with self._session.get(
                self.base_url, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True
            ) as resp:
                await resp.read()   # discard – we just want the cookie/session warm-up
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.error(
                "Cannot reach device at %s: %s (%r)",
                self.base_url,
                exc.__class__.__name__,
                exc,
            )
            raise

        # POST credentials – always use the well-known WEM field names.
        try:
            async with self._session.post(
                f"{self.base_url}/login.html",
                data={"user": self.username, "pass": self.password},
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.error("Login POST failed: %s (%r)", exc.__class__.__name__, exc)
            raise

        _LOGGER.info("Login POST completed for %s (session cookie accepted)", self.base_url)

    # ------------------------------------------------------------------
    # Fetch a stack (with retries for incomplete pages)
    # ------------------------------------------------------------------

    async def _fetch_stack_html(self, stack: str) -> Optional[str]:
        """Fetch one stack page and return raw HTML (with retries/relogin)."""
        url = f"{self.base_url}/settings_export.html?stack={stack}"

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 401:
                        _LOGGER.info("Session expired, re-logging in")
                        await self._login()
                        continue

                    if resp.status != 200:
                        body_preview = (await resp.text())[:240].replace("\n", " ").replace("\r", " ")
                        _LOGGER.warning(
                            "Stack %s: HTTP %d (attempt %d/%d) url=%s preview=%s",
                            stack[:30], resp.status, attempt, self.max_retries, url, body_preview,
                        )
                    else:
                        html = await resp.text()
                        soup = BeautifulSoup(html, "lxml")
                        if is_login_page(soup):
                            _LOGGER.info(
                                "Stack %s returned the login page, retrying after re-login",
                                stack[:30],
                            )
                            await self._login()
                            continue

                        parsed = parse_settings_page(html, stack)
                        if parsed is not None:
                            return html

                        html_preview = html[:240].replace("\n", " ").replace("\r", " ")
                        _LOGGER.warning(
                            "Stack %s: page could not be parsed (attempt %d/%d), retrying in %ds, preview=%s",
                            stack[:30], attempt, self.max_retries, self.retry_interval, html_preview,
                        )

            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Timeout on stack %s (attempt %d/%d)", stack[:30], attempt, self.max_retries
                )
            except aiohttp.ClientError as exc:
                _LOGGER.error("HTTP error on stack %s: %s", stack[:30], exc)
                try:
                    await self._login()
                except Exception:
                    pass

            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_interval)

        _LOGGER.error("Stack %s: gave up after %d attempts", stack[:30], self.max_retries)
        return None

    async def _fetch_stack(self, stack: str) -> Optional[List[ParsedParameter]]:
        html = await self._fetch_stack_html(stack)
        if html is None:
            return None

        result = parse_settings_page(html, stack)
        if result is not None:
            _LOGGER.debug("Stack %s: parsed %d parameter(s)", stack[:30], len(result))
            return result
        return None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover_all(self) -> None:
        _LOGGER.info("Discovery: probing %d stack entries", len(self.entries))
        for stack in self.entries:
            await self._discover_stack(stack)
            await asyncio.sleep(0.5)   # brief pause between requests
        _LOGGER.info("Discovery complete – %d parameter(s) found", len(self._parameters))

    async def _discover_stack(self, stack: str) -> bool:
        params = await self._fetch_stack(stack)

        if params is None:
            _LOGGER.error(
                "Discovery failed for stack %s (no parseable response after retries)",
                stack[:40],
            )
            key = self.make_key(stack, "discovery_failed")
            self._parameters[key] = ParameterInfo(
                stack=stack,
                param_id="discovery_failed",
                name=f"Discovery Failed ({stack[:20]}…)",
                param_type="readonly",
                discovery_failed=True,
            )
            return False

        if len(params) == 0:
            _LOGGER.warning(
                "Discovery returned 0 parameters for stack %s (page loaded but parser found nothing)",
                stack[:40],
            )

        await self._store_discovered_params(stack, params)
        return True

    async def _bootstrap_entries_from_home(self) -> int:
        """Bootstrap initial stack entries from home page when none are configured."""
        added = 0
        known: set[str] = set(self.entries)

        try:
            async with self._session.get(
                f"{self.base_url}/home.html",
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                html = await resp.text()

                if resp.status != 200:
                    preview = html[:240].replace("\n", " ").replace("\r", " ")
                    _LOGGER.warning(
                        "Bootstrap home read failed: HTTP %d host=%s preview=%s",
                        resp.status,
                        self.ip_address,
                        preview,
                    )
                    return 0

                soup = BeautifulSoup(html, "lxml")
                if is_login_page(soup):
                    _LOGGER.warning(
                        "Bootstrap home read returned login page for host=%s",
                        self.ip_address,
                    )
                    return 0

                for stack in _extract_stack_links_from_html(html):
                    if stack not in known:
                        known.add(stack)
                        self.entries.append(stack)
                        added += 1

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.warning(
                "Bootstrap from home page failed for host=%s: %s",
                self.ip_address,
                exc,
            )
            return 0

        if added == 0:
            _LOGGER.warning(
                "Bootstrap found no stack links on home page for host=%s",
                self.ip_address,
            )

        return added

    async def async_rediscover_stack(self, stack: str) -> None:
        """Public API: re-run discovery for one stack (e.g. service call)."""
        _LOGGER.info("Re-discovering stack: %s", stack[:40])
        added_entry = False
        if stack not in self.entries:
            self.entries.append(stack)
            added_entry = True

        success = await self._discover_stack(stack)
        if success and self.hass is not None and self.config_entry is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                options={
                    **self.config_entry.options,
                    CONF_ENTRIES: "\n".join(self.entries),
                },
            )
        elif not success and added_entry:
            self.entries.remove(stack)

    def _start_selected_rediscover_retry(self) -> None:
        """Start background rediscovery loop for the selected stack from options."""
        if self.hass is None or self.config_entry is None:
            return

        selected_stack = str(self.config_entry.options.get(CONF_REDISCOVER_STACK, "") or "").strip()
        if not selected_stack:
            return

        if self._rediscover_retry_task and not self._rediscover_retry_task.done():
            self._rediscover_retry_task.cancel()

        self._rediscover_retry_task = asyncio.ensure_future(
            self._rediscover_selected_stack_loop(selected_stack)
        )

    async def _rediscover_selected_stack_loop(self, selected_stack: str) -> None:
        """Retry rediscovery for a selected stack every 20s until success or option changes."""
        try:
            while True:
                if self.config_entry is None or self.hass is None:
                    return

                current_selected = str(
                    self.config_entry.options.get(CONF_REDISCOVER_STACK, "") or ""
                ).strip()
                if current_selected != selected_stack:
                    _LOGGER.info(
                        "Stopping rediscover retry for stack %s because selection changed",
                        selected_stack[:40],
                    )
                    return

                success = await self._discover_stack(selected_stack)
                if success:
                    _LOGGER.info(
                        "Rediscover retry for stack %s succeeded; clearing selection",
                        selected_stack[:40],
                    )
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        options={
                            **self.config_entry.options,
                            CONF_REDISCOVER_STACK: "",
                        },
                    )
                    return

                await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "Rediscover retry loop failed for stack %s",
                selected_stack[:40],
            )

    async def async_initialize_entries(
        self,
        scan_interval_seconds: int = 10,
        max_entries: int = 500,
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None] | None]] = None,
    ) -> Dict[str, int]:
        """
        One-time recursive stack discovery helper.

        - Starts from configured `self.entries`.
        - Recursively extracts nested `stack=` links from fetched pages.
        - Enforces one stack fetch every `scan_interval_seconds`.
        """
        was_running = self._running and self._poll_task is not None
        if was_running:
            await self._stop_polling_for_maintenance()

        effective_interval = max(5, int(scan_interval_seconds))
        queue: List[str] = list(self.entries)
        known: set[str] = set(self.entries)
        new_entries: List[str] = []
        attempts: Dict[str, int] = {stack: 0 for stack in queue}
        details: List[Dict[str, Any]] = []
        processed = 0
        failed = 0
        last_fetch_ts: Optional[float] = None
        root_entries: List[str] = list(queue)
        root_index: Dict[str, int] = {stack: idx + 1 for idx, stack in enumerate(root_entries)}
        root_done: set[str] = set()
        current_root_stack: str = ""

        async def _emit_progress(current_menu: str = "") -> None:
            if progress_callback is None:
                return

            current_idx = root_index.get(current_root_stack, 0)
            payload: Dict[str, Any] = {
                "root_total": len(root_entries),
                "root_done": len(root_done),
                "root_current_index": current_idx,
                "root_current_stack": current_root_stack,
                "root_current_menu": current_menu,
                "processed": processed,
                "max_entries": max_entries,
            }
            try:
                maybe_coro = progress_callback(payload)
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception as exc:
                _LOGGER.debug("Progress callback failed: %s", exc)

        _LOGGER.info(
            "Initialization scan started (seed=%d, interval=%ds, max=%d)",
            len(queue), effective_interval, max_entries,
        )

        if not queue:
            try:
                async with self._session.get(
                    f"{self.base_url}/home.html",
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                ) as resp:
                    home_html = await resp.text()
                    for nested_stack in _extract_stack_links_from_html(home_html):
                        if nested_stack not in known:
                            known.add(nested_stack)
                            self.entries.append(nested_stack)
                            new_entries.append(nested_stack)
                            queue.append(nested_stack)
                            attempts[nested_stack] = 0
                _LOGGER.info(
                    "Initialization scan bootstrapped %d entries from home page",
                    len(queue),
                )
                root_entries = list(queue)
                root_index = {stack: idx + 1 for idx, stack in enumerate(root_entries)}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.warning(
                    "Initialization scan could not bootstrap entries from home page: %s",
                    exc,
                )

            if not queue:
                self.entries = list(DEFAULT_INITIAL_ENTRIES)
                queue = list(self.entries)
                known = set(self.entries)
                attempts = {stack: 0 for stack in queue}
                root_entries = list(queue)
                root_index = {stack: idx + 1 for idx, stack in enumerate(root_entries)}
                _LOGGER.warning(
                    "Initialization scan bootstrap found no menu links; falling back to default initial entries (%d stacks)",
                    len(queue),
                )

        try:
            await _emit_progress()
            while queue and processed < max_entries:
                stack = queue.pop(0)
                if stack in root_index:
                    current_root_stack = stack
                    await _emit_progress()

                if last_fetch_ts is not None and effective_interval > 0:
                    wait_seconds = effective_interval - (monotonic() - last_fetch_ts)
                    if wait_seconds > 0:
                        await asyncio.sleep(wait_seconds)

                html = await self._fetch_stack_html(stack)
                last_fetch_ts = monotonic()
                processed += 1

                if html is None:
                    attempts[stack] = attempts.get(stack, 0) + 1
                    if attempts[stack] < 3 and processed < max_entries:
                        _LOGGER.warning(
                            "Initialization scan transient failure on stack %s (attempt %d/3), re-queueing",
                            stack[:40],
                            attempts[stack],
                        )
                        queue.append(stack)
                        details.append(
                            {
                                "stack": stack,
                                "menu": "(retry)",
                                "status": "retry",
                                "parsed_params": 0,
                                "found_nested": 0,
                            }
                        )
                    else:
                        failed += 1
                        details.append(
                            {
                                "stack": stack,
                                "menu": "(failed)",
                                "status": "failed",
                                "parsed_params": 0,
                                "found_nested": 0,
                            }
                        )
                    if stack in root_index:
                        root_done.add(stack)
                        await _emit_progress("(failed)")
                    continue

                nested = _extract_stack_links_from_html(html)
                nested_added = 0
                for nested_stack in nested:
                    if nested_stack not in known:
                        known.add(nested_stack)
                        self.entries.append(nested_stack)
                        new_entries.append(nested_stack)
                        queue.append(nested_stack)
                        attempts[nested_stack] = 0
                        nested_added += 1

                params = parse_settings_page(html, stack)
                if params is None:
                    failed += 1
                    details.append(
                        {
                            "stack": stack,
                            "menu": "(unparsed)",
                            "status": "failed",
                            "parsed_params": 0,
                            "found_nested": nested_added,
                        }
                    )
                    if stack in root_index:
                        root_done.add(stack)
                        await _emit_progress("(unparsed)")
                    continue
                await self._store_discovered_params(stack, params)

                menu_name = "(unknown)"
                if params:
                    first_name = str(params[0].name or "").strip()
                    if first_name:
                        menu_name = first_name.split(",")[0].strip()

                details.append(
                    {
                        "stack": stack,
                        "menu": menu_name,
                        "status": "ok",
                        "parsed_params": len(params),
                        "found_nested": nested_added,
                    }
                )

                if stack in root_index:
                    root_done.add(stack)
                    await _emit_progress(menu_name)

            await _emit_progress()
            _LOGGER.info(
                "Initialization scan done: processed=%d, new_entries=%d, failed=%d, total_entries=%d",
                processed,
                len(new_entries),
                failed,
                len(self.entries),
            )
            return {
                "processed": processed,
                "new_entries": len(new_entries),
                "failed": failed,
                "total_entries": len(self.entries),
                "details": details,
            }
        finally:
            if was_running:
                self._running = True
                self._poll_task = asyncio.ensure_future(self._polling_loop())

    async def _stop_polling_for_maintenance(self) -> None:
        """Temporarily stop background polling while running maintenance tasks."""
        self._running = False
        self._write_event.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _store_discovered_params(self, stack: str, params: List[ParsedParameter]) -> None:
        """Persist parsed parameters and notify listeners."""
        # Remove stale failure marker once discovery succeeds again.
        failure_key = self.make_key(stack, "discovery_failed")
        self._parameters.pop(failure_key, None)
        saw_usable_value = False

        for p in params:
            key = self.make_key(stack, p.param_id)
            self._parameters[key] = ParameterInfo(
                stack=stack,
                param_id=p.param_id,
                name=p.name,
                param_type=p.param_type,
                unit=p.unit,
                min_value=p.min_value,
                max_value=p.max_value,
                step=p.step,
                options=p.options,
                form_field_name=p.form_field_name,
                write_action=p.write_action,
                write_fields=p.write_fields,
                current_value=p.current_value,
                last_updated=datetime.now(),
            )
            _LOGGER.info(
                "Discovered  %-50s  [%s]  value=%s  %s",
                p.name, p.param_type, p.current_value, p.unit,
            )
            if self._has_usable_value(p.current_value):
                saw_usable_value = True

        if saw_usable_value:
            await self._mark_successful_read()

        # Notify HA platforms (or standalone consumers) about new parameters
        for cb in self._new_param_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(stack, params)
                else:
                    cb(stack, params)
            except Exception as exc:
                _LOGGER.error("Error in new-param callback: %s", exc)

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    async def _polling_loop(self) -> None:
        _LOGGER.info(
            "Polling loop started – interval %ds, %d entries",
            self.cycle_interval, len(self.entries),
        )
        while self._running:
            try:
                # --- process any pending writes BEFORE the next regular poll ---
                if not self._write_queue.empty():
                    saved_index = self._current_index
                    await self._drain_write_queue()
                    self._current_index = saved_index   # resume from same position
                    # Wait a full cycle interval before resuming normal polls
                    self._write_event.clear()
                    await self._interruptible_sleep(self.cycle_interval)
                    continue

                # --- regular poll ---
                if self.entries:
                    stack = self.entries[self._current_index]
                    await self._poll_stack(stack)
                    self._current_index = (self._current_index + 1) % len(self.entries)

                    # After one full pass over all entries, retry missing values once.
                    if self._current_index == 0 and self._missing_values_retry:
                        await self._retry_missing_values_once()

                # A write may have arrived while we were polling – handle it
                # immediately instead of sleeping first.
                if not self._write_queue.empty():
                    continue

                # --- wait cycle interval (can be interrupted by write event) ---
                self._write_event.clear()
                await self._interruptible_sleep(self.cycle_interval)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.error("Unexpected error in polling loop: %s", exc, exc_info=True)
                await asyncio.sleep(self.cycle_interval)

    async def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep for `seconds` but wake up early if _write_event is set."""
        try:
            await asyncio.wait_for(asyncio.shield(self._write_event.wait()), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # Polling a single stack
    # ------------------------------------------------------------------

    def _known_param_ids_for_stack(self, stack: str) -> set[str]:
        """Return all known parameter IDs for a stack (excluding failure markers)."""
        return {
            info.param_id
            for info in self._parameters.values()
            if info.stack == stack and not info.discovery_failed
        }

    async def _retry_missing_values_once(self) -> None:
        """Retry parameters that were missing in the last full polling pass."""
        retry_map = {stack: set(ids) for stack, ids in self._missing_values_retry.items() if ids}
        if not retry_map:
            return

        total_missing = sum(len(ids) for ids in retry_map.values())
        _LOGGER.warning(
            "Retry pass for missing values started: stacks=%d params=%d",
            len(retry_map),
            total_missing,
        )

        unresolved: Dict[str, set[str]] = {}
        for stack, missing_ids in retry_map.items():
            seen_ids = await self._poll_stack(stack, track_missing=False)
            if seen_ids is None:
                unresolved[stack] = missing_ids
            else:
                still_missing = missing_ids - seen_ids
                if still_missing:
                    unresolved[stack] = still_missing

            if self.retry_interval > 0:
                await asyncio.sleep(self.retry_interval)

        self._missing_values_retry = unresolved
        if unresolved:
            _LOGGER.warning(
                "Retry pass finished with unresolved values: stacks=%d params=%d",
                len(unresolved),
                sum(len(ids) for ids in unresolved.values()),
            )
        else:
            _LOGGER.info("Retry pass finished: all previously missing values were refreshed")

    async def _poll_stack(self, stack: str, track_missing: bool = True) -> Optional[set[str]]:
        known_param_ids = self._known_param_ids_for_stack(stack)
        has_known_params = bool(known_param_ids)

        params = await self._fetch_stack(stack)
        if params is None and has_known_params:
            # Keep existing entries and retry aggressively for transient read/parse failures.
            for attempt in range(2, 11):
                _LOGGER.warning(
                    "Polling stack %s failed; retrying (%d/10)",
                    stack[:40],
                    attempt,
                )
                await asyncio.sleep(self.retry_interval)
                params = await self._fetch_stack(stack)
                if params is not None:
                    break

        if params is None:
            if has_known_params:
                _LOGGER.error(
                    "Polling stack %s failed after 10 attempts; keeping last known values",
                    stack[:40],
                )
            return None

        seen_param_ids: set[str] = set()
        usable_param_ids: set[str] = set()

        for p in params:
            seen_param_ids.add(p.param_id)
            key = self.make_key(stack, p.param_id)
            info = self._parameters.get(key)
            if info is None:
                # Newly appeared parameter – add it
                info = ParameterInfo(
                    stack=stack,
                    param_id=p.param_id,
                    name=p.name,
                    param_type=p.param_type,
                    unit=p.unit,
                    min_value=p.min_value,
                    max_value=p.max_value,
                    step=p.step,
                    options=p.options,
                    form_field_name=p.form_field_name,
                    write_action=p.write_action,
                    write_fields=p.write_fields,
                )
                self._parameters[key] = info
            else:
                # Update metadata for existing parameter (enables correction of scaled values)
                info.unit = p.unit
                info.min_value = p.min_value
                info.max_value = p.max_value
                info.step = p.step
                info.options = p.options
                info.form_field_name = p.form_field_name

            info.write_action = p.write_action
            info.write_fields = p.write_fields

            old_val = info.current_value
            if self._has_usable_value(p.current_value):
                usable_param_ids.add(p.param_id)
                info.current_value = p.current_value
                info.last_updated = datetime.now()
                await self._mark_successful_read(info.last_updated)

                if old_val != p.current_value:
                    _LOGGER.debug(
                        "Value changed: %s  %s → %s", p.name, old_val, p.current_value
                    )
                    await self._fire_callbacks(key)
            else:
                _LOGGER.debug(
                    "Ignoring unusable value for %s on stack %s; keeping last known value=%s",
                    p.name,
                    stack[:40],
                    old_val,
                )

        if track_missing and known_param_ids:
            missing = known_param_ids - usable_param_ids
            if missing:
                self._missing_values_retry[stack] = missing
                _LOGGER.warning(
                    "Polling stack %s missing %d known value(s); scheduling retry pass",
                    stack[:40],
                    len(missing),
                )
            else:
                self._missing_values_retry.pop(stack, None)

        return usable_param_ids

    # ------------------------------------------------------------------
    # Value callbacks
    # ------------------------------------------------------------------

    async def _fire_callbacks(self, key: str) -> None:
        for cb in list(self._callbacks.get(key, [])):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb()
                else:
                    cb()
            except Exception as exc:
                _LOGGER.error("Error in update callback for %s: %s", key, exc)

    # ------------------------------------------------------------------
    # Write queue
    # ------------------------------------------------------------------

    async def request_write(self, stack: str, param_id: str, value: Any) -> None:
        """
        Queue a write request from an HA entity (or standalone script).
        Any existing queued write for the same parameter is superseded.
        """
        # Remove stale requests for the same parameter
        new_q: asyncio.Queue = asyncio.Queue()
        while not self._write_queue.empty():
            item = self._write_queue.get_nowait()
            if not (item["stack"] == stack and item["param_id"] == param_id):
                await new_q.put(item)
        # Transfer remaining items
        while not new_q.empty():
            await self._write_queue.put(new_q.get_nowait())

        await self._write_queue.put({"stack": stack, "param_id": param_id, "value": value})
        self._write_event.set()   # interrupt the sleep in the polling loop

    async def _drain_write_queue(self) -> None:
        while not self._write_queue.empty():
            try:
                req = self._write_queue.get_nowait()
                await self._execute_write(req["stack"], req["param_id"], req["value"])
                self._write_queue.task_done()
            except asyncio.QueueEmpty:
                break

    # ------------------------------------------------------------------
    # Write + verify
    # ------------------------------------------------------------------

    async def _execute_write(self, stack: str, param_id: str, value: Any) -> None:
        key = self.make_key(stack, param_id)
        info = self._parameters.get(key)
        if info is None:
            _LOGGER.error("Write: parameter not found: %s", key)
            return

        url = f"{self.base_url}/settings_export.html?stack={stack}"
        field_name = info.form_field_name or "value"
        
        # Check if this parameter uses 10x scaling (e.g., 20.5°C stored as 205)
        write_value: Any = value
        if info.write_fields and info.write_fields.get("__scaling_factor__") == "10":
            # Apply scaling: multiply by 10 when writing
            try:
                write_value = int(round(float(value) * 10))
                _LOGGER.debug("Applying 10x scaling for write: %s → %s", value, write_value)
            except (ValueError, TypeError):
                pass
        else:
            # Avoid sending trailing .0 for integer-like numbers (device expects exact option values)
            try:
                numeric_value = float(value)
                if numeric_value.is_integer():
                    write_value = int(numeric_value)
            except (ValueError, TypeError):
                pass

        for attempt in range(1, DEFAULT_MAX_WRITE_RETRIES + 1):
            # 1. Write
            try:
                payload = dict(info.write_fields or {})
                # Remove marker fields before sending
                payload.pop("__scaling_factor__", None)
                payload[info.form_field_name or field_name] = str(write_value)
                async with self._session.post(
                    f"{self.base_url}/{(info.write_action or 'pro_save.html').lstrip('/')}",
                    data=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 401:
                        await self._login()
                    elif resp.status not in (200, 302, 303):
                        _LOGGER.warning(
                            "Write %s=%s: HTTP %d (attempt %d/%d)",
                            info.name, value, resp.status, attempt, DEFAULT_MAX_WRITE_RETRIES,
                        )
                    else:
                        _LOGGER.info("Write %s = %s (attempt %d)", info.name, value, attempt)
            except aiohttp.ClientError as exc:
                _LOGGER.error("Write failed: %s", exc)
                try:
                    await self._login()
                except Exception:
                    pass

            # 2. Wait before verifying
            await asyncio.sleep(self.cycle_interval)

            # 3. Read back
            params = await self._fetch_stack(stack)
            if params is None:
                _LOGGER.warning("Write verify: could not read back %s", info.name)
                if attempt < DEFAULT_MAX_WRITE_RETRIES:
                    await asyncio.sleep(self.retry_interval)
                continue

            verified = False
            for p in params:
                if p.param_id == param_id:
                    if _values_equal(p.current_value, value):
                        _LOGGER.info("Write verified: %s = %s", info.name, p.current_value)
                        info.current_value = p.current_value
                        info.last_updated = datetime.now()
                        await self._mark_successful_read(info.last_updated)
                        await self._fire_callbacks(key)
                        verified = True
                    else:
                        _LOGGER.warning(
                            "Write verify mismatch: %s expected=%s got=%s (attempt %d/%d)",
                            info.name, value, p.current_value, attempt, DEFAULT_MAX_WRITE_RETRIES,
                        )
                    break

            if verified:
                return
            if attempt < DEFAULT_MAX_WRITE_RETRIES:
                await asyncio.sleep(self.retry_interval)

        _LOGGER.error(
            "Write failed after %d attempts: %s = %s",
            DEFAULT_MAX_WRITE_RETRIES, info.name, value,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _values_equal(a: Any, b: Any) -> bool:
    if a == b:
        return True
    try:
        return float(str(a).replace(",", ".")) == float(str(b).replace(",", "."))
    except (ValueError, TypeError):
        return str(a).strip().lower() == str(b).strip().lower()


def _parse_entries(raw: str) -> List[str]:
    """
    Accept entries as:
      - newline-separated lines
      - semicolon-separated
    Each line is one stack (may itself be comma-separated IDs).
    Leading/trailing whitespace and empty lines are ignored.
    """
    lines: List[str] = []
    for sep in ("\n", ";"):
        if sep in raw:
            lines = [l.strip() for l in raw.split(sep) if l.strip()]
            return lines
    # Single entry
    return [raw.strip()] if raw.strip() else []


def _extract_stack_links_from_html(html: str) -> List[str]:
    """Extract stack values from any page markup that references `stack=`.

    The WEM UI sometimes renders navigation entries as normal links, but on
    some firmware versions the same targets appear in onclick handlers,
    hidden form actions, or inline scripts instead of plain <a href> tags.
    """
    soup = BeautifulSoup(html, "lxml")
    found: List[str] = []
    seen: set[str] = set()

    def _add_stack(raw_stack: str) -> None:
        stack = unquote(raw_stack).strip()
        if stack and stack not in seen:
            seen.add(stack)
            found.append(stack)

    # 1) Structured URLs in all tag attributes.
    for tag in soup.find_all(True):
        for attr_value in tag.attrs.values():
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                if not isinstance(value, str) or "stack=" not in value:
                    continue
                parsed = urlparse(value)
                query = parse_qs(parsed.query)
                for raw_stack in query.get("stack", []):
                    _add_stack(raw_stack)

    # 2) Raw HTML fallback for inline scripts / unusual markup.
    for match in re.finditer(r"stack=([^\"'&<>\s]+)", html):
        _add_stack(match.group(1))

    return found
