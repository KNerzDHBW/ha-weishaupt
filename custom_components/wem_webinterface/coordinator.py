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
import inspect
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
    DOMAIN,
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
    last_real_value: Any = None
    last_updated: Optional[datetime] = None
    has_successful_read: bool = False
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
        self._last_successful_sensor_name: Optional[str] = None
        self._last_read_error: Optional[str] = None

        self._write_event = asyncio.Event()
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._poll_task: Optional[asyncio.Task] = None
        self._rediscover_retry_task: Optional[asyncio.Task] = None
        self._current_index: int = 0
        self._running: bool = False
        self._missing_values_retry: Dict[str, set[str]] = {}
        self._min_read_interval_seconds: int = 5
        self._last_read_request_ts: Optional[float] = None
        self._read_rate_lock = asyncio.Lock()
        self._consecutive_read_failures: int = 0
        self._cache_store = None

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

    @property
    def last_successful_sensor_name(self) -> Optional[str]:
        return self._last_successful_sensor_name

    @property
    def consecutive_read_failures(self) -> int:
        return self._consecutive_read_failures

    @property
    def last_read_error(self) -> Optional[str]:
        return self._last_read_error

    @staticmethod
    def _has_usable_value(value: Any) -> bool:
        """Return True if a parsed value should replace the current entity state."""
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        return True

    @staticmethod
    async def _await_result(value: Any) -> Any:
        """Await one nested awaitable if a mocked async method returns a coroutine."""
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _number_supports_default_minus_200(info: ParameterInfo) -> bool:
        """Return True if -200 is within the configured HA number limits."""
        if info.min_value is not None and -200 < info.min_value:
            return False
        if info.max_value is not None and -200 > info.max_value:
            return False
        return True

    def _initial_default_value(self, info: ParameterInfo) -> Any:
        """Build a startup-only placeholder value for known parameters."""
        if info.param_type == "number":
            if self._number_supports_default_minus_200(info):
                return -200.0
            if self._has_usable_value(info.last_real_value):
                return info.last_real_value
            if info.min_value is not None:
                return info.min_value
            return 0.0

        if info.param_type == "select":
            options = list(info.options or [])
            if "" in options:
                return ""
            if self._has_usable_value(info.last_real_value):
                return str(info.last_real_value)
            if options:
                return options[0]
            return ""

        # Read-only/text-like states can be shown empty in HA until first poll update.
        return ""

    def _stack_is_fully_known(self, stack: str) -> bool:
        """Return True when every known parameter in stack has been read successfully before."""
        infos = [
            info
            for info in self._parameters.values()
            if info.stack == stack and not info.discovery_failed
        ]
        return bool(infos) and all(info.has_successful_read for info in infos)

    def _get_cache_store(self):
        """Return HA storage backend used for persistent parameter metadata cache."""
        if self.hass is None or self.config_entry is None or not hasattr(self.hass, "config"):
            return None

        if self._cache_store is not None:
            return self._cache_store

        try:
            from homeassistant.helpers.storage import Store
        except Exception:
            return None

        self._cache_store = Store(
            self.hass,
            1,
            f"{DOMAIN}_{self.config_entry.entry_id}_parameter_cache",
        )
        return self._cache_store

    async def _load_cached_parameters(self) -> int:
        """Load parameter metadata and last known states from persistent HA storage."""
        store = self._get_cache_store()
        if store is None:
            return 0

        try:
            data = await store.async_load()
        except Exception as exc:
            _LOGGER.warning("Failed to load parameter cache: %s", exc)
            return 0

        if not isinstance(data, dict):
            return 0

        raw_params = data.get("parameters")
        if not isinstance(raw_params, list):
            return 0

        loaded = 0
        for raw in raw_params:
            if not isinstance(raw, dict):
                continue

            stack = str(raw.get("stack") or "")
            param_id = str(raw.get("param_id") or "")
            if not stack or not param_id:
                continue

            cached_value = raw.get("current_value")
            last_real_value = raw.get("last_real_value", cached_value)
            has_successful_read = raw.get("has_successful_read")
            if has_successful_read is None:
                has_successful_read = self._has_usable_value(last_real_value)

            last_updated = None
            raw_last_updated = raw.get("last_updated")
            if isinstance(raw_last_updated, str) and raw_last_updated:
                try:
                    last_updated = datetime.fromisoformat(raw_last_updated)
                except ValueError:
                    last_updated = None

            info = ParameterInfo(
                stack=stack,
                param_id=param_id,
                name=str(raw.get("name") or param_id),
                param_type=str(raw.get("param_type") or "readonly"),
                unit=str(raw.get("unit") or ""),
                min_value=raw.get("min_value"),
                max_value=raw.get("max_value"),
                step=raw.get("step"),
                options=list(raw.get("options") or []),
                form_field_name=raw.get("form_field_name"),
                write_action=raw.get("write_action"),
                write_fields=dict(raw.get("write_fields") or {}),
                current_value=None,
                last_real_value=last_real_value,
                last_updated=last_updated,
                has_successful_read=bool(has_successful_read),
                discovery_failed=False,
            )

            info.current_value = self._initial_default_value(info)
            if not self._has_usable_value(info.current_value):
                info.current_value = cached_value
            if not self._has_usable_value(info.current_value):
                info.current_value = self._initial_default_value(info)

            key = self.make_key(stack, param_id)
            self._parameters[key] = info
            loaded += 1

        if loaded:
            _LOGGER.info("Loaded %d parameter(s) from persistent cache", loaded)

        return loaded

    async def _save_cached_parameters(self) -> None:
        """Persist discovered parameter metadata and last known values."""
        store = self._get_cache_store()
        if store is None:
            return

        payload: list[dict[str, Any]] = []
        for info in self._parameters.values():
            if info.discovery_failed:
                continue

            last_real_value = info.last_real_value
            if not self._has_usable_value(last_real_value) and self._has_usable_value(info.current_value):
                last_real_value = info.current_value

            payload.append(
                {
                    "stack": info.stack,
                    "param_id": info.param_id,
                    "name": info.name,
                    "param_type": info.param_type,
                    "unit": info.unit,
                    "min_value": info.min_value,
                    "max_value": info.max_value,
                    "step": info.step,
                    "options": list(info.options or []),
                    "form_field_name": info.form_field_name,
                    "write_action": info.write_action,
                    "write_fields": dict(info.write_fields or {}),
                    "current_value": info.current_value,
                    "last_real_value": last_real_value,
                    "last_updated": info.last_updated.isoformat() if info.last_updated else None,
                    "has_successful_read": bool(info.has_successful_read),
                }
            )

        try:
            await store.async_save({"parameters": payload})
        except Exception as exc:
            _LOGGER.warning("Failed to save parameter cache: %s", exc)

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

    async def _notify_status_callbacks(self) -> None:
        """Notify diagnostic listeners about coordinator-level state changes."""
        for cb in list(self._status_callbacks):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb()
                else:
                    cb()
            except Exception as exc:
                _LOGGER.error("Error in status callback: %s", exc)

    async def _mark_successful_read(
        self,
        when: Optional[datetime] = None,
        sensor_name: Optional[str] = None,
    ) -> None:
        """Track the last time any parameter was read successfully."""
        self._last_successful_read = when or datetime.now()
        if sensor_name:
            self._last_successful_sensor_name = sensor_name
        self._consecutive_read_failures = 0
        self._last_read_error = None
        await self._notify_status_callbacks()

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
        await self._await_result(self._create_session())
        loaded_cached_params = await self._load_cached_parameters()
        try:
            await self._await_result(self._check_ip_reachability())
            _LOGGER.info("Reachability check (DNS/ping) passed for host=%s", self.ip_address)
            await self._await_result(self._check_web_port_reachability())
            _LOGGER.info("Port 80 check passed for host=%s", self.ip_address)
            await self._await_result(self._login())
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

            if loaded_cached_params == 0:
                await self._await_result(self._discover_all())
            else:
                _LOGGER.info(
                    "Skipping startup discovery because %d cached parameter(s) were loaded; new values will be read in the normal polling cycle",
                    loaded_cached_params,
                )
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
        await self._save_cached_parameters()
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Session / login
    # ------------------------------------------------------------------

    async def _create_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True, quote_cookie=False)
        self._session = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=jar,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WEM-HA/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    async def _verify_authenticated_session(self) -> bool:
        """Return True if the current session can access protected pages."""
        probe_urls: List[str] = []
        if self.entries:
            probe_urls.append(f"{self.base_url}/settings_export.html?stack={self.entries[0]}")
        probe_urls.append(f"{self.base_url}/settings_export.html")
        probe_urls.append(f"{self.base_url}/home.html")

        for url in probe_urls:
            try:
                await self._respect_min_read_interval("login verification probe")
                async with self._session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                ) as resp:
                    html = await resp.text()
                    if resp.status != 200:
                        continue
                    soup = BeautifulSoup(html, "lxml")
                    if not is_login_page(soup):
                        return True
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue

        return False

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

    async def _respect_min_read_interval(self, purpose: str = "") -> None:
        """Enforce a global minimum interval between read requests."""
        min_interval = max(0, int(self._min_read_interval_seconds))
        if min_interval <= 0:
            return

        async with self._read_rate_lock:
            now = monotonic()
            if self._last_read_request_ts is not None:
                elapsed = now - self._last_read_request_ts
                wait_seconds = min_interval - elapsed
                if wait_seconds > 0:
                    _LOGGER.debug(
                        "Read rate-limit: waiting %.2fs before %s",
                        wait_seconds,
                        purpose or "next read request",
                    )
                    await asyncio.sleep(wait_seconds)
                    now = monotonic()
            self._last_read_request_ts = now

    async def _handle_failed_read_attempt(self, reason: str, stack: str = "") -> None:
        """Track failed reads and trigger re-login every 5th consecutive failure."""
        self._consecutive_read_failures += 1
        failures = self._consecutive_read_failures
        self._last_read_error = f"{reason} ({stack[:40]})" if stack else reason
        if failures % 5 == 0:
            _LOGGER.warning(
                "Read failed %d times consecutively (%s); forcing re-login",
                failures,
                reason,
            )
            try:
                await self._login()
            except Exception as exc:
                self._last_read_error = f"forced-relogin-failed: {exc}"
                _LOGGER.error("Forced re-login failed after read failures: %s", exc)
        await self._notify_status_callbacks()

    def _handle_successful_read_attempt(self) -> None:
        """Reset failure counter after any successful read attempt."""
        self._consecutive_read_failures = 0

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
                headers={
                    "Referer": f"{self.base_url}/login.html",
                    "Origin": self.base_url,
                },
            ) as resp:
                await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.error("Login POST failed: %s (%r)", exc.__class__.__name__, exc)
            raise

        authenticated = await self._verify_authenticated_session()
        if not authenticated:
            raise PermissionError(
                f"Login failed for {self.base_url}: still receiving login page after POST."
            )

        _LOGGER.info("Login completed for %s (authenticated session verified)", self.base_url)

    # ------------------------------------------------------------------
    # Fetch a stack (with retries for incomplete pages)
    # ------------------------------------------------------------------

    async def _fetch_stack_html(self, stack: str) -> Optional[str]:
        """Fetch one stack page and return raw HTML (with retries/relogin)."""
        url = f"{self.base_url}/settings_export.html?stack={stack}"

        for attempt in range(1, self.max_retries + 1):
            try:
                await self._respect_min_read_interval(f"stack read {stack[:30]}")
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 401:
                        _LOGGER.info("Session expired while reading stack %s", stack[:30])
                        await self._handle_failed_read_attempt("http-401", stack)
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
                                "Stack %s returned the login page",
                                stack[:30],
                            )
                            await self._handle_failed_read_attempt("login-page", stack)
                            continue

                        parsed = parse_settings_page(html, stack)
                        if parsed is not None:
                            self._handle_successful_read_attempt()
                            return html

                        html_preview = html[:240].replace("\n", " ").replace("\r", " ")
                        _LOGGER.warning(
                            "Stack %s: page could not be parsed (attempt %d/%d), retrying in %ds, preview=%s",
                            stack[:30], attempt, self.max_retries, self.retry_interval, html_preview,
                        )
                        await self._handle_failed_read_attempt("unparseable-page", stack)

            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Timeout on stack %s (attempt %d/%d)", stack[:30], attempt, self.max_retries
                )
                await self._handle_failed_read_attempt("timeout", stack)
            except aiohttp.ClientError as exc:
                _LOGGER.error("HTTP error on stack %s: %s", stack[:30], exc)
                try:
                    await self._handle_failed_read_attempt("http-client-error", stack)
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
            if self._stack_is_fully_known(stack):
                _LOGGER.info(
                    "Discovery: skipping startup read for stack %s (all values known from cache)",
                    stack[:40],
                )
                continue
            await self._discover_stack(stack)
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
            await self._respect_min_read_interval("home bootstrap read")
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
                await self._respect_min_read_interval("init-scan home bootstrap read")
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
        last_usable_name: Optional[str] = None
        persist_cache = False

        for p in params:
            key = self.make_key(stack, p.param_id)
            info = self._parameters.get(key)
            if info is None:
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
                persist_cache = True
            else:
                if info.name != p.name:
                    info.name = p.name
                    persist_cache = True
                if info.param_type != p.param_type:
                    info.param_type = p.param_type
                    persist_cache = True
                if info.unit != p.unit:
                    info.unit = p.unit
                    persist_cache = True
                if info.min_value != p.min_value:
                    info.min_value = p.min_value
                    persist_cache = True
                if info.max_value != p.max_value:
                    info.max_value = p.max_value
                    persist_cache = True
                if info.step != p.step:
                    info.step = p.step
                    persist_cache = True
                if info.options != p.options:
                    info.options = p.options
                    persist_cache = True
                if info.form_field_name != p.form_field_name:
                    info.form_field_name = p.form_field_name
                    persist_cache = True
                if info.write_action != p.write_action:
                    info.write_action = p.write_action
                    persist_cache = True
                if info.write_fields != p.write_fields:
                    info.write_fields = p.write_fields
                    persist_cache = True

            if self._has_usable_value(p.current_value):
                info.current_value = p.current_value
                info.last_real_value = p.current_value
                info.last_updated = datetime.now()
                if not info.has_successful_read:
                    info.has_successful_read = True
                    persist_cache = True
            elif info.current_value is None:
                info.current_value = self._initial_default_value(info)

            _LOGGER.info(
                "Discovered  %-50s  [%s]  value=%s  %s",
                p.name, p.param_type, p.current_value, p.unit,
            )
            if self._has_usable_value(p.current_value):
                saw_usable_value = True
                last_usable_name = p.name

        if saw_usable_value:
            await self._mark_successful_read(sensor_name=last_usable_name)

        if persist_cache:
            await self._save_cached_parameters()

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
            metadata_changed = False
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
                metadata_changed = True
            else:
                # Update metadata for existing parameter (enables correction of scaled values)
                if info.unit != p.unit:
                    metadata_changed = True
                info.unit = p.unit
                if info.min_value != p.min_value:
                    metadata_changed = True
                info.min_value = p.min_value
                if info.max_value != p.max_value:
                    metadata_changed = True
                info.max_value = p.max_value
                if info.step != p.step:
                    metadata_changed = True
                info.step = p.step
                if info.options != p.options:
                    metadata_changed = True
                info.options = p.options
                if info.form_field_name != p.form_field_name:
                    metadata_changed = True
                info.form_field_name = p.form_field_name

            if info.write_action != p.write_action:
                metadata_changed = True
            if info.write_fields != p.write_fields:
                metadata_changed = True
            info.write_action = p.write_action
            info.write_fields = p.write_fields

            persist_cache = False
            if metadata_changed:
                persist_cache = True

            old_val = info.current_value
            if self._has_usable_value(p.current_value):
                usable_param_ids.add(p.param_id)
                info.current_value = p.current_value
                info.last_real_value = p.current_value
                info.last_updated = datetime.now()
                if not info.has_successful_read:
                    info.has_successful_read = True
                    persist_cache = True
                await self._mark_successful_read(info.last_updated, info.name)

                if old_val != p.current_value:
                    _LOGGER.debug(
                        "Value changed: %s  %s → %s", p.name, old_val, p.current_value
                    )
                    await self._fire_callbacks(key)
            else:
                if old_val is None:
                    info.current_value = self._initial_default_value(info)
                _LOGGER.debug(
                    "Ignoring unusable value for %s on stack %s; keeping last known value=%s",
                    p.name,
                    stack[:40],
                    old_val,
                )

            if persist_cache:
                await self._save_cached_parameters()

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
                        await self._mark_successful_read(info.last_updated, info.name)
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
