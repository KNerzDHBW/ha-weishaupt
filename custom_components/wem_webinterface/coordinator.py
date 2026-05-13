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
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    CONF_CYCLE_INTERVAL,
    CONF_IP_ADDRESS,
    CONF_MAX_RETRIES,
    CONF_PASSWORD,
    CONF_RETRY_INTERVAL,
    CONF_USERNAME,
    CONF_ENTRIES,
    DEFAULT_CYCLE_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_WRITE_RETRIES,
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

        self._write_event = asyncio.Event()
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._poll_task: Optional[asyncio.Task] = None
        self._current_index: int = 0
        self._running: bool = False

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Login, discover all stacks, then start background polling."""
        await self._create_session()
        try:
            await self._check_ip_reachability()
            await self._check_web_port_reachability()
            await self._login()
            await self._discover_all()
            self._running = True
            self._poll_task = asyncio.ensure_future(self._polling_loop())
        except Exception:
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
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ConnectionError(
                "Ping command not available on this system. Cannot check host reachability first."
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
        """Perform form-based login and persist the session cookie."""
        login_url = self.base_url
        try:
            async with self._session.get(
                login_url, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True
            ) as resp:
                login_html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.error(
                "Cannot reach device at %s (login GET): %s (%r)",
                login_url,
                exc.__class__.__name__,
                exc,
            )
            raise

        soup = BeautifulSoup(login_html, "lxml")
        form = soup.find("form")

        if not form:
            # Some devices serve the form only on /login.html.
            explicit_login_url = f"{self.base_url}/login.html"
            try:
                async with self._session.get(
                    explicit_login_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                ) as resp:
                    login_html = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.error(
                    "Cannot reach explicit login page at %s: %s (%r)",
                    explicit_login_url,
                    exc.__class__.__name__,
                    exc,
                )
                raise

            soup = BeautifulSoup(login_html, "lxml")
            form = soup.find("form")

        hidden_fields: Dict[str, str] = {}
        action = f"{self.base_url}/login.html"
        user_field = "user"
        pass_field = "pass"

        if form:
            # Collect hidden fields (CSRF tokens etc.)
            for inp in form.find_all("input", {"type": "hidden"}):
                n = inp.get("name")
                if n:
                    hidden_fields[n] = inp.get("value", "")

            # Identify username / password field names
            for inp in form.find_all("input"):
                itype = (inp.get("type") or "text").lower()
                iname = (inp.get("name") or "").lower()
                if not iname:
                    continue

                if itype in ("text", "email") or any(
                    k in iname for k in ("user", "username", "login", "name")
                ):
                    user_field = inp.get("name", user_field)
                elif itype == "password" or any(k in iname for k in ("pass", "pwd")):
                    pass_field = inp.get("name", pass_field)

            raw_action = form.get("action", "/")
            if raw_action.startswith("http"):
                action = raw_action
            else:
                candidate = raw_action.strip()
                if candidate in ("", "/"):
                    action = f"{self.base_url}/login.html"
                else:
                    action = self.base_url + "/" + candidate.lstrip("/")

        try:
            # Attempt 1: exact legacy WEM form expected by many devices.
            primary_data: Dict[str, str] = dict(hidden_fields)
            primary_data["user"] = self.username
            primary_data["pass"] = self.password

            async with self._session.post(
                f"{self.base_url}/login.html",
                data=primary_data,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                await resp.text()

            if await self._is_authenticated_session():
                _LOGGER.info("Login successful to %s (user/pass via /login.html)", self.base_url)
                return

            # Attempt 2: detected field/action names for firmware variants.
            fallback_data: Dict[str, str] = dict(hidden_fields)
            fallback_data[user_field] = self.username
            fallback_data[pass_field] = self.password

            async with self._session.post(
                action,
                data=fallback_data,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                await resp.text()

            if await self._is_authenticated_session():
                _LOGGER.info("Login successful to %s (form-detected fields)", self.base_url)
                return

            raise PermissionError(
                f"Invalid username/password for WEM device {self.ip_address}"
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.error("Login POST failed: %s (%r)", exc.__class__.__name__, exc)
            raise

    async def _is_authenticated_session(self) -> bool:
        """Validate that the current session can access a protected page.

        Uses a stack URL when available because /home.html on many WEM
        firmware versions redirects to /login.html regardless of auth state.
        Falls back to trusting the session cookie when no stack is configured.
        """
        if not self.entries:
            # No stack to probe – trust that the POST set a valid cookie.
            # The next actual fetch will re-login if needed.
            return True

        probe_url = f"{self.base_url}/settings_export.html?stack={self.entries[0]}"
        try:
            async with self._session.get(
                probe_url,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as probe_resp:
                probe_html = await probe_resp.text()
                probe_soup = BeautifulSoup(probe_html, "lxml")
                probe_path = urlparse(str(probe_resp.url)).path
                unauthenticated = (
                    probe_resp.status in (401, 403)
                    or probe_path.endswith("/login.html")
                    or is_login_page(probe_soup)
                )
                return not unauthenticated
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Network hiccup – assume session is valid, polling will fix it.
            return True

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
                        _LOGGER.warning(
                            "Stack %s: HTTP %d (attempt %d/%d)",
                            stack[:30], resp.status, attempt, self.max_retries,
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

                        _LOGGER.debug(
                            "Stack %s: page incomplete (attempt %d/%d), retrying in %ds",
                            stack[:30], attempt, self.max_retries, self.retry_interval,
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

    async def _discover_stack(self, stack: str) -> None:
        params = await self._fetch_stack(stack)

        if params is None:
            _LOGGER.error("Discovery failed for stack %s", stack[:40])
            key = self.make_key(stack, "discovery_failed")
            self._parameters[key] = ParameterInfo(
                stack=stack,
                param_id="discovery_failed",
                name=f"Discovery Failed ({stack[:20]}…)",
                param_type="readonly",
                discovery_failed=True,
            )
            return

        await self._store_discovered_params(stack, params)

    async def async_rediscover_stack(self, stack: str) -> None:
        """Public API: re-run discovery for one stack (e.g. service call)."""
        _LOGGER.info("Re-discovering stack: %s", stack[:40])
        await self._discover_stack(stack)

    async def async_initialize_entries(
        self,
        scan_interval_seconds: int = 10,
        max_entries: int = 500,
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
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.warning(
                    "Initialization scan could not bootstrap entries from home page: %s",
                    exc,
                )

        try:
            while queue and processed < max_entries:
                stack = queue.pop(0)

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

    async def _poll_stack(self, stack: str) -> None:
        params = await self._fetch_stack(stack)
        if params is None:
            return

        for p in params:
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

            info.write_action = p.write_action
            info.write_fields = p.write_fields

            old_val = info.current_value
            info.current_value = p.current_value
            info.last_updated = datetime.now()

            if old_val != p.current_value:
                _LOGGER.debug(
                    "Value changed: %s  %s → %s", p.name, old_val, p.current_value
                )
                await self._fire_callbacks(key)

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

        for attempt in range(1, DEFAULT_MAX_WRITE_RETRIES + 1):
            # 1. Write
            try:
                payload = dict(info.write_fields or {})
                payload[info.form_field_name or field_name] = str(value)
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
    """Extract all stack values from settings_export links in a page."""
    soup = BeautifulSoup(html, "lxml")
    found: List[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "settings_export.html" not in href or "stack=" not in href:
            continue
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        for raw_stack in query.get("stack", []):
            stack = unquote(raw_stack).strip()
            if stack and stack not in seen:
                seen.add(stack)
                found.append(stack)

    return found
