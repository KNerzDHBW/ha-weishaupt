#!/usr/bin/env python3
"""Standalone WEM crawler (initialization + polling) for local experiments.

Rules implemented:
- All web requests are spaced by at least 5 seconds globally.
- Every request uses retries because pages can fail or load incompletely.
- On startup:
  - Login
  - Open /settings_export.html
  - Traverse menus two levels deep
  - Read sub entries and values
  - On first run only: open editor links to capture possible value ranges/options
- After initialization:
  - Poll continuously by opening level-2 pages only (no editor pages)
  - Print read values to console
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import math
import re
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from bs4 import BeautifulSoup

BASE_URL = "http://heizung.home"
USERNAME = "admin"
PASSWORD = "C4v_mxfD43Lk"

MIN_REQUEST_GAP_SECONDS = 4.0
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 4
RETRY_SLEEP_SECONDS = 3
POLL_CYCLE_SLEEP_SECONDS = 0
LOGIN_ROUNDS = 5


@dataclasses.dataclass
class SubEntry:
    level1: str
    level2: str
    name: str
    value: Any
    unit: str
    source_stack: str
    editor_stack: Optional[str] = None
    range_info: Optional[Dict[str, Any]] = None
    range_logged: bool = False

    @property
    def key(self) -> str:
        return f"{self.level1} | {self.level2} | {self.name}"


class WemInitPoll:
    def __init__(self, min_request_gap_seconds: float = MIN_REQUEST_GAP_SECONDS) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_lock = asyncio.Lock()
        self._last_request_ts: float = 0.0
        self._min_request_gap_seconds: float = max(0.0, float(min_request_gap_seconds))
        self._status_line_len: int = 0
        self._entries: Dict[str, SubEntry] = {}
        self._level2_stacks: Dict[str, str] = {}

    async def __aenter__(self) -> "WemInitPoll":
        self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def run(self) -> None:
        await self.login()
        await self.initialize_once()
        await self.poll_forever()

    async def _respect_min_gap(self, next_read_label: str) -> None:
        async with self._request_lock:
            now = time.monotonic()
            wait_for = self._min_request_gap_seconds - (now - self._last_request_ts)
            if wait_for > 0:
                await self._countdown_inline(wait_for, f"Next read ({next_read_label}) in")
            self._last_request_ts = time.monotonic()

    async def _countdown_inline(self, seconds: float, prefix: str) -> None:
        """Show countdown in a single line that is overwritten by next output."""
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            shown = int(math.ceil(remaining))
            self._status_write(f"{prefix} {shown:02d}s")
            sleep_for = min(1.0, remaining)
            await asyncio.sleep(sleep_for)
            remaining -= sleep_for

        # Clear countdown line before normal line-based output continues.
        self._status_clear()

    async def _request_text(self, method: str, url: str, **kwargs: Any) -> str:
        assert self._session is not None
        last_error: Optional[Exception] = None
        read_label = self._read_label_from_url(method, url)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._status_write(f"[http] {read_label} (attempt {attempt}/{MAX_RETRIES})")
                await self._respect_min_gap(read_label)
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
                async with self._session.request(
                    method,
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    **kwargs,
                ) as response:
                    text = await response.text()
                    self._status_write(f"[http] {read_label} -> {response.status}")
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status} at {url}")
                    if self._looks_incomplete(text):
                        raise RuntimeError(f"Incomplete page at {url}")
                    self._status_clear()
                    return text
            except Exception as exc:  # noqa: BLE001 - retry intentionally broad
                last_error = exc
                self._status_clear()
                print(f"[retry {attempt}/{MAX_RETRIES}] {method} {url} failed: {exc}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_SLEEP_SECONDS)

        raise RuntimeError(f"Request failed after retries: {method} {url} ({last_error})")

    def _read_label_from_url(self, method: str, url: str) -> str:
        parsed = urlparse(url)
        path = (parsed.path or "").rstrip("/")
        page = path.split("/")[-1] if path else "root"
        query = parse_qs(parsed.query)
        stack = query.get("stack", [""])[0].strip()
        if stack:
            return f"{method.upper()} {page} stack={self._stack_tail(stack)}"
        return f"{method.upper()} {page}"

    @staticmethod
    def _looks_incomplete(html: str) -> bool:
        if not html or len(html.strip()) < 80:
            return True
        lower = html.lower()
        if "loading..." in lower or "bitte warten" in lower or "wird geladen" in lower:
            if len(html) < 1000:
                return True
        return False

    @staticmethod
    def _is_login_page(html: str) -> bool:
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        if form is None:
            return False
        action = str(form.get("action") or "").lower().strip()
        has_pw = form.find("input", {"type": "password"}) is not None
        if not has_pw:
            return False

        body_text = soup.get_text(" ", strip=True).lower()
        login_words = ("login", "einloggen", "anmelden")
        action_looks_login = any(token in action for token in ("login", "index"))
        text_looks_login = any(word in body_text for word in login_words)
        return action_looks_login or text_looks_login

    @staticmethod
    def _looks_authenticated_content(html: str) -> bool:
        if not html:
            return False
        lower = html.lower()
        if "settings_export.html?stack=" in lower:
            return True
        if "browseobj" in lower and "nav-link" in lower:
            return True
        return False

    async def login(self) -> None:
        login_page_url = f"{BASE_URL}/index.html"
        verify_urls = [f"{BASE_URL}/settings_export.html"]

        for round_idx in range(1, LOGIN_ROUNDS + 1):
            print(f"[login] round {round_idx}/{LOGIN_ROUNDS}")
            login_page_html = await self._request_text("GET", login_page_url)

            # Some firmware variants may already redirect to authenticated content.
            if self._looks_authenticated_content(login_page_html) and not self._is_login_page(login_page_html):
                print("[ok] Login already active")
                return

            login_post_candidates: List[str] = []
            soup = BeautifulSoup(login_page_html, "lxml")
            form = soup.find("form")

            payload: Dict[str, str] = {}
            user_field_name = "user"
            pass_field_name = "pass"

            if form is not None:
                action = str(form.get("action") or "").strip()
                if action:
                    if action.startswith("http://") or action.startswith("https://"):
                        login_post_candidates.append(action)
                    else:
                        login_post_candidates.append(f"{BASE_URL}/{action.lstrip('/')}")

                text_like = form.find("input", {"type": re.compile(r"^(text|email)$", re.I)})
                if text_like is None:
                    text_like = form.find("input", attrs={"name": re.compile(r"user|login|name", re.I)})
                password_like = form.find("input", {"type": "password"})

                if text_like is not None and text_like.get("name"):
                    user_field_name = str(text_like.get("name"))
                if password_like is not None and password_like.get("name"):
                    pass_field_name = str(password_like.get("name"))

                for inp in form.find_all("input"):
                    name = str(inp.get("name") or "").strip()
                    if not name:
                        continue
                    input_type = str(inp.get("type") or "text").lower()
                    if input_type == "hidden":
                        payload[name] = str(inp.get("value") or "")

            # Fallbacks for firmware variants where form-action is missing/empty.
            for fallback_url in (f"{BASE_URL}/index.html", f"{BASE_URL}/login.html", f"{BASE_URL}/"):
                if fallback_url not in login_post_candidates:
                    login_post_candidates.append(fallback_url)

            payload[user_field_name] = USERNAME
            payload[pass_field_name] = PASSWORD

            # Send common aliases as compatibility fallback for variant field names.
            for alias in ("user", "username", "login", "name"):
                payload.setdefault(alias, USERNAME)
            for alias in ("pass", "password", "pwd"):
                payload.setdefault(alias, PASSWORD)

            last_error: Optional[Exception] = None
            posted_ok = False
            for post_url in login_post_candidates:
                try:
                    _ = await self._request_text(
                        "POST",
                        post_url,
                        data=payload,
                        headers={"Referer": login_page_url, "Origin": BASE_URL},
                    )
                    posted_ok = True
                    break
                except Exception as exc:  # noqa: BLE001 - login fallback sequence
                    last_error = exc
                    print(f"[login] POST fallback failed at {post_url}: {exc}")

            if not posted_ok:
                if round_idx == LOGIN_ROUNDS:
                    raise RuntimeError(f"Login POST failed for all targets: {last_error}")
                await asyncio.sleep(RETRY_SLEEP_SECONDS)
                continue

            authenticated = False
            for verify_url in verify_urls:
                try:
                    print(f"[login] verifying session via {verify_url}")
                    check = await self._request_text("GET", verify_url)
                except Exception:
                    continue
                if self._looks_authenticated_content(check):
                    authenticated = True
                    break
                if not self._is_login_page(check):
                    authenticated = True
                    break

            if authenticated:
                print("[ok] Login successful")
                return

            if round_idx < LOGIN_ROUNDS:
                print("[login] verification still unauthenticated, retrying complete login round...")
                await asyncio.sleep(RETRY_SLEEP_SECONDS)

        raise PermissionError("Login failed: session verification stayed unauthenticated")

    async def initialize_once(self) -> None:
        print("[init] Reading root menu")
        root_url = f"{BASE_URL}/settings_export.html"
        root_html = await self._request_text("GET", root_url)

        # If the session dropped, try one explicit re-login and re-read.
        if self._is_login_page(root_html):
            print("[init] Root page is login page, retrying login once...")
            await self.login()
            root_html = await self._request_text("GET", root_url)

        level1_stacks = self._extract_stack_links(root_html)

        # WEM can occasionally return a partial-but-200 root page. Retry root read.
        if not level1_stacks:
            for retry in range(1, 3):
                print(f"[init] No level-1 links on root, retrying root read ({retry}/2)")
                root_html = await self._request_text("GET", f"{root_url}?_ts={int(time.time())}")
                level1_stacks = self._extract_stack_links(root_html)
                if level1_stacks:
                    break

        if not level1_stacks:
            raise RuntimeError(
                "[init] Found 0 level-1 stack links on settings_export.html; "
                "aborting instead of continuing with empty poll list"
            )

        root_names = self._extract_stack_name_map(root_html)
        print(f"[init] Found {len(level1_stacks)} level-1 stack links")
        for stack in level1_stacks:
            menu_name = root_names.get(stack, self._stack_tail(stack))
            print(f"[init] Menü: {menu_name}")

        for level1_stack in level1_stacks:
            level1_url = f"{BASE_URL}/settings_export.html?stack={level1_stack}"
            level1_html = await self._request_text("GET", level1_url)
            active_labels = self._active_labels_from_html(level1_html)
            level1_name = active_labels[-1] if active_labels else root_names.get(level1_stack, self._stack_tail(level1_stack))
            level2_stacks_raw = self._extract_stack_links(level1_html)
            level2_stacks: List[str] = []
            seen_level2: set[str] = set()
            for stack in level2_stacks_raw:
                if stack == level1_stack:
                    continue
                if not self._is_child_stack(level1_stack, stack):
                    continue
                if stack in seen_level2:
                    continue
                seen_level2.add(stack)
                level2_stacks.append(stack)

            # Sometimes the page returns partial content although HTTP is 200.
            # Retry if no second level was found unexpectedly.
            if not level2_stacks:
                for retry in range(1, 3):
                    print(
                        f"[init] {level1_name}: no second level shown, retrying level-1 read ({retry}/2)"
                    )
                    level1_html = await self._request_text(
                        "GET", f"{level1_url}&_ts={int(time.time())}"
                    )
                    level2_stacks_raw = self._extract_stack_links(level1_html)
                    level2_stacks = []
                    seen_level2 = set()
                    for stack in level2_stacks_raw:
                        if stack == level1_stack:
                            continue
                        if not self._is_child_stack(level1_stack, stack):
                            continue
                        if stack in seen_level2:
                            continue
                        seen_level2.add(stack)
                        level2_stacks.append(stack)
                    if level2_stacks:
                        break

            level2_names = self._extract_stack_name_map(level1_html)

            if level2_stacks:
                print(f"[init] {level1_name}: found {len(level2_stacks)} second-level entries")
                for level2_stack in level2_stacks:
                    l2_name = level2_names.get(level2_stack, self._stack_tail(level2_stack))
                    print(f"[init]   Untermenü: {l2_name}")
            else:
                print(
                    f"[init] {level1_name}: no second level, reading values directly from level 1"
                )
                self._level2_stacks[f"{level1_name} | "] = level1_stack
                await self._read_level2_page(
                    level1_name=level1_name,
                    level2_name="",
                    level2_stack=level1_stack,
                    init_mode=True,
                )
                continue

            for level2_stack in level2_stacks:
                level2_name = level2_names.get(level2_stack, self._stack_tail(level2_stack))
                self._level2_stacks[f"{level1_name} | {level2_name}"] = level2_stack
                await self._read_level2_page(
                    level1_name=level1_name,
                    level2_name=level2_name,
                    level2_stack=level2_stack,
                    init_mode=True,
                )

        print(f"[init] Collected {len(self._entries)} entries")

    async def poll_forever(self) -> None:
        print("[poll] Starting continuous poll (level-2 pages only)")
        while True:
            for key, level2_stack in list(self._level2_stacks.items()):
                parts = key.split(" | ", maxsplit=1)
                if len(parts) != 2:
                    continue
                level1_name, level2_name = parts
                await self._read_level2_page(
                    level1_name=level1_name,
                    level2_name=level2_name,
                    level2_stack=level2_stack,
                    init_mode=False,
                )

            if POLL_CYCLE_SLEEP_SECONDS > 0:
                await asyncio.sleep(POLL_CYCLE_SLEEP_SECONDS)

    async def _read_level2_page(
        self,
        level1_name: str,
        level2_name: str,
        level2_stack: str,
        init_mode: bool,
    ) -> None:
        url = f"{BASE_URL}/settings_export.html?stack={level2_stack}"
        html = await self._request_text("GET", url)

        # Some firmware replies with the main menu (or parts of it) instead of the requested page.
        if self._looks_like_menu_echo_page(html, level1_name, level2_name):
            for retry in range(1, 3):
                label_for_log = f"{level1_name}, {level2_name}" if level2_name else level1_name
                print(f"[open] {label_for_log}: page looked like menu echo, retrying read ({retry}/2)")
                html = await self._request_text("GET", f"{url}&_ts={int(time.time())}")
                if not self._looks_like_menu_echo_page(html, level1_name, level2_name):
                    break

        menu_labels, submenu_labels = self._extract_menu_and_submenu_labels(html)
        if menu_labels:
            print(f"[open] Menueeintraege: {', '.join(menu_labels)}")
        if submenu_labels:
            print(f"[open] Untermenueeintraege: {', '.join(submenu_labels)}")

        active_labels = self._active_labels_from_html(html)
        if active_labels:
            if level2_name and len(active_labels) >= 2:
                level1_name = active_labels[-2]
                level2_name = active_labels[-1]
            elif not level2_name:
                level1_name = active_labels[-1]

        if level2_name:
            print(f"[open] {level1_name}, {level2_name}")
        else:
            print(f"[open] {level1_name}")

        sub_entries = self._extract_sub_entries(
            html=html,
            level1_name=level1_name,
            level2_name=level2_name,
            level2_stack=level2_stack,
        )

        # If entries are unexpectedly missing, retry this menu page.
        if not sub_entries:
            for retry in range(1, 3):
                label_for_log = f"{level1_name}, {level2_name}" if level2_name else level1_name
                print(f"[open] {label_for_log}: no entries shown, retrying read ({retry}/2)")
                html = await self._request_text("GET", f"{url}&_ts={int(time.time())}")
                menu_labels, submenu_labels = self._extract_menu_and_submenu_labels(html)
                if menu_labels:
                    print(f"[open] Menueeintraege: {', '.join(menu_labels)}")
                if submenu_labels:
                    print(f"[open] Untermenueeintraege: {', '.join(submenu_labels)}")
                sub_entries = self._extract_sub_entries(
                    html=html,
                    level1_name=level1_name,
                    level2_name=level2_name,
                    level2_stack=level2_stack,
                )
                if sub_entries:
                    break

        label = f"{level1_name}, {level2_name}" if level2_name else level1_name
        print(f"[open] Ergebnis: {label} -> {len(sub_entries)} Einträge")

        for item in sub_entries:
            existing = self._entries.get(item.key)
            if existing is None:
                self._entries[item.key] = item
            else:
                existing.value = item.value
                existing.unit = item.unit
                existing.editor_stack = item.editor_stack or existing.editor_stack
                existing.range_info = item.range_info or existing.range_info
                item = existing

            if init_mode and item.editor_stack and item.range_info is None:
                item.range_info = await self._read_editor_range(item.editor_stack)

            if item.range_info and not item.range_logged:
                self._print_range_info(item)
                item.range_logged = True

            self._print_item(item)

    async def _read_editor_range(self, editor_stack: str) -> Optional[Dict[str, Any]]:
        """Open editor page once and extract numeric/select range metadata."""
        html = await self._request_text("GET", f"{BASE_URL}/settings_export.html?stack={editor_stack}")
        soup = BeautifulSoup(html, "lxml")

        form = soup.find("form")
        if form is None:
            return None

        select = form.find("select")
        if select is not None:
            options = []
            for opt in select.find_all("option"):
                txt = self._normalize(opt.get_text(" ", strip=True))
                if txt:
                    options.append(txt)
            if options:
                return {"type": "select", "options": options}
            return None

        number_input = form.find("input", {"type": re.compile(r"^(number|text)$", re.I)})
        if number_input is None:
            return None

        min_value = self._to_float(number_input.get("min"))
        max_value = self._to_float(number_input.get("max"))
        step_value = self._to_float(number_input.get("step"))

        for hidden in form.find_all("input", {"type": "hidden"}):
            name = str(hidden.get("name") or "").lower()
            raw = hidden.get("value")
            if min_value is None and "min" in name:
                min_value = self._to_float(raw)
            if max_value is None and "max" in name:
                max_value = self._to_float(raw)
            if step_value is None and ("step" in name or "inc" in name):
                step_value = self._to_float(raw)

        return {
            "type": "number",
            "min": min_value,
            "max": max_value,
            "step": step_value,
        }

    def _extract_sub_entries(
        self,
        html: str,
        level1_name: str,
        level2_name: str,
        level2_stack: str,
    ) -> List[SubEntry]:
        soup = BeautifulSoup(html, "lxml")
        blocks = soup.select("div.nav-link.browseobj")
        result: List[SubEntry] = []

        for block in blocks:
            label_node = block.find("h5")
            if label_node is None:
                continue

            name = self._normalize(label_node.get_text(" ", strip=True))
            if not name:
                continue

            inline_form = block.find("form")
            inline_range_info: Optional[Dict[str, Any]] = None

            if inline_form is not None:
                value, unit, inline_range_info = self._extract_inline_form_value_and_range(
                    block=block,
                    form=inline_form,
                )
                # Inline form means this entry is editable on the current page.
                editor_stack = level2_stack
            else:
                block_text = self._normalize(block.get_text(" ", strip=True))
                value_text = block_text[len(name):].strip() if block_text.startswith(name) else ""
                value, unit = self._split_value_unit(value_text)
                editor_stack = self._extract_first_stack_from_tag(block)

            result.append(
                SubEntry(
                    level1=level1_name,
                    level2=level2_name,
                    name=name,
                    value=value,
                    unit=unit,
                    source_stack=level2_stack,
                    editor_stack=editor_stack,
                    range_info=inline_range_info,
                )
            )

        # Some pages render writable/readable values as anchor links (no inline form).
        # Those links are parameter entries, not menu levels, when they are direct stack children.
        for link in soup.select("a.nav-link.browseobj"):
            link_stack = self._extract_first_stack_from_tag(link)
            if not link_stack or not self._is_child_stack(level2_stack, link_stack):
                continue

            label_node = link.find("h5")
            if label_node is None:
                continue
            name = self._normalize(label_node.get_text(" ", strip=True))
            if not name:
                continue

            link_text = self._normalize(link.get_text(" ", strip=True))
            value_text = link_text[len(name):].strip() if link_text.startswith(name) else ""
            value, unit = self._split_value_unit(value_text)

            entry = SubEntry(
                level1=level1_name,
                level2=level2_name,
                name=name,
                value=value,
                unit=unit,
                source_stack=level2_stack,
                editor_stack=link_stack,
            )
            if any(existing.key == entry.key for existing in result):
                continue
            result.append(entry)

        if not result:
            result = self._extract_readonly_sub_entries_fallback(
                soup=soup,
                level1_name=level1_name,
                level2_name=level2_name,
                level2_stack=level2_stack,
            )

        return result

    def _extract_readonly_sub_entries_fallback(
        self,
        soup: BeautifulSoup,
        level1_name: str,
        level2_name: str,
        level2_stack: str,
    ) -> List[SubEntry]:
        """Fallback for pages that show read-only values as plain text without links."""
        result: List[SubEntry] = []
        seen_keys: set[str] = set()

        for label_node in soup.find_all("h5"):
            name = self._normalize(label_node.get_text(" ", strip=True))
            if not name:
                continue

            classes = " ".join(label_node.get("class", []))
            if "activeobj" in classes:
                continue

            container = label_node.find_parent(["div", "li", "tr", "td", "section", "article"])
            if container is None:
                continue

            container_text = self._normalize(container.get_text(" ", strip=True))
            if not container_text.startswith(name):
                continue

            value_text = container_text[len(name):].strip()
            if not value_text:
                continue

            value, unit = self._split_value_unit(value_text)
            entry = SubEntry(
                level1=level1_name,
                level2=level2_name,
                name=name,
                value=value,
                unit=unit,
                source_stack=level2_stack,
                editor_stack=None,
            )
            if entry.key in seen_keys:
                continue
            seen_keys.add(entry.key)
            result.append(entry)

        return result

    @staticmethod
    def _extract_stack_links(html: str) -> List[str]:
        soup = BeautifulSoup(html, "lxml")
        found: List[str] = []
        seen: set[str] = set()

        def _add(raw_stack: str) -> None:
            value = unquote(raw_stack).strip()
            if value and value not in seen:
                seen.add(value)
                found.append(value)

        for tag in soup.find_all(True):
            for attr_value in tag.attrs.values():
                values = attr_value if isinstance(attr_value, list) else [attr_value]
                for value in values:
                    if not isinstance(value, str) or "stack=" not in value:
                        continue
                    parsed = urlparse(value)
                    query = parse_qs(parsed.query)
                    for raw in query.get("stack", []):
                        _add(raw)

        for match in re.finditer(r"stack=([^\"'&<>\s]+)", html):
            _add(match.group(1))

        return found

    def _extract_stack_name_map(self, html: str) -> Dict[str, str]:
        """Best-effort mapping stack id -> visible menu label from current page."""
        soup = BeautifulSoup(html, "lxml")
        mapping: Dict[str, str] = {}

        for tag in soup.find_all(True):
            stack_value: Optional[str] = None
            for attr_value in tag.attrs.values():
                values = attr_value if isinstance(attr_value, list) else [attr_value]
                for value in values:
                    if not isinstance(value, str) or "stack=" not in value:
                        continue
                    parsed = urlparse(value)
                    query = parse_qs(parsed.query)
                    stacks = query.get("stack", [])
                    if stacks:
                        stack_value = unquote(stacks[0]).strip()
                        break
                if stack_value:
                    break

            if not stack_value or stack_value in mapping:
                continue

            label_node = tag.find("h5")
            if label_node is None:
                continue
            label = self._normalize(label_node.get_text(" ", strip=True))
            if label:
                mapping[stack_value] = label

        return mapping

    def _extract_menu_and_submenu_labels(self, html: str) -> tuple[List[str], List[str]]:
        """Return visible first and second menu-column labels from current page."""
        soup = BeautifulSoup(html, "lxml")
        columns = soup.select("main .container .row > div.col-3")
        parsed_columns: List[List[str]] = []

        for col in columns:
            labels: List[str] = []
            for node in col.select("a.nav-link.browseobj h5, div.nav-link.browseobj h5"):
                text = self._normalize(node.get_text(" ", strip=True))
                if text:
                    labels.append(text)
            if labels:
                parsed_columns.append(labels)

        menu_labels = parsed_columns[0] if len(parsed_columns) >= 1 else []
        submenu_labels = parsed_columns[1] if len(parsed_columns) >= 2 else []
        return menu_labels, submenu_labels

    def _looks_like_menu_echo_page(self, html: str, expected_level1: str, expected_level2: str) -> bool:
        """Detect cases where requested page accidentally returns root menu structures."""
        menu_labels, submenu_labels = self._extract_menu_and_submenu_labels(html)
        if not menu_labels or not submenu_labels:
            return False

        menu_set = set(menu_labels)
        submenu_set = set(submenu_labels)
        overlap = len(menu_set & submenu_set)

        # Exact/near-exact duplication of main menu as submenu column.
        if submenu_set and overlap == len(submenu_set) and len(submenu_set) >= 2:
            return True

        # Requested submenu should appear in submenu column on a healthy page.
        if expected_level2 and expected_level2 not in submenu_set:
            if overlap >= max(1, len(submenu_set) // 2):
                return True

        # A submenu list that directly includes the main menu item is suspicious.
        if expected_level2 and expected_level1 and expected_level1 in submenu_set:
            return True

        return False

    def _active_labels_from_html(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "lxml")
        labels: List[str] = []
        for node in soup.select(".nav-link.browseobj.activeobj h5"):
            text = self._normalize(node.get_text(" ", strip=True))
            if text:
                labels.append(text)
        return labels

    def _extract_first_stack_from_tag(self, tag) -> Optional[str]:
        for attr_value in tag.attrs.values():
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                if not isinstance(value, str) or "stack=" not in value:
                    continue
                parsed = urlparse(value)
                query = parse_qs(parsed.query)
                stacks = query.get("stack", [])
                if stacks:
                    return unquote(stacks[0]).strip()

        for node in tag.find_all(True):
            for attr_value in node.attrs.values():
                values = attr_value if isinstance(attr_value, list) else [attr_value]
                for value in values:
                    if not isinstance(value, str) or "stack=" not in value:
                        continue
                    parsed = urlparse(value)
                    query = parse_qs(parsed.query)
                    stacks = query.get("stack", [])
                    if stacks:
                        return unquote(stacks[0]).strip()

        return None

    def _extract_inline_form_value_and_range(self, block, form) -> tuple[Any, str, Optional[Dict[str, Any]]]:
        select = form.find("select")
        if select is not None:
            options: List[str] = []
            selected_text = ""
            for opt in select.find_all("option"):
                txt = self._normalize(opt.get_text(" ", strip=True))
                if txt:
                    options.append(txt)
                if opt.has_attr("selected") and txt:
                    selected_text = txt
            if not selected_text and options:
                selected_text = options[0]
            value, unit = self._split_value_unit(selected_text)
            return value, unit, {"type": "select", "options": options}

        number_input = form.find("input", {"type": re.compile(r"^(number|text)$", re.I)})
        if number_input is not None:
            current_raw = str(number_input.get("value") or "").strip()
            value, unit = self._split_value_unit(current_raw)
            min_value = self._to_float(number_input.get("min"))
            max_value = self._to_float(number_input.get("max"))
            step_value = self._to_float(number_input.get("step"))
            for hidden in form.find_all("input", {"type": "hidden"}):
                name = str(hidden.get("name") or "").lower()
                raw = hidden.get("value")
                if min_value is None and "min" in name:
                    min_value = self._to_float(raw)
                if max_value is None and "max" in name:
                    max_value = self._to_float(raw)
                if step_value is None and ("step" in name or "inc" in name):
                    step_value = self._to_float(raw)
            return value, unit, {
                "type": "number",
                "min": min_value,
                "max": max_value,
                "step": step_value,
            }

        block_text = self._normalize(block.get_text(" ", strip=True))
        label = block.find("h5")
        label_text = self._normalize(label.get_text(" ", strip=True)) if label is not None else ""
        value_text = block_text[len(label_text):].strip() if label_text and block_text.startswith(label_text) else ""
        value, unit = self._split_value_unit(value_text)
        return value, unit, None

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(str(value).replace(",", ".").strip())
        except (ValueError, TypeError):
            return None

    def _split_value_unit(self, text: str) -> tuple[Any, str]:
        if not text:
            return "", ""
        m = re.match(r"^([+-]?[0-9]+(?:[.,][0-9]+)?)\s*(.*)$", text)
        if m:
            number = self._to_float(m.group(1))
            unit = self._normalize(m.group(2))
            if number is not None:
                return number, unit
        return self._normalize(text), ""

    @staticmethod
    def _stack_tail(stack: str) -> str:
        parts = [p.strip() for p in stack.split(",") if p.strip()]
        if not parts:
            return stack
        return parts[-1]

    @staticmethod
    def _is_child_stack(parent_stack: str, candidate_stack: str) -> bool:
        """Return True if candidate is a direct child of parent stack."""
        parent_parts = [p.strip() for p in parent_stack.split(",") if p.strip()]
        candidate_parts = [p.strip() for p in candidate_stack.split(",") if p.strip()]
        if len(candidate_parts) != len(parent_parts) + 1:
            return False
        return candidate_parts[: len(parent_parts)] == parent_parts

    def _print_range_info(self, item: SubEntry) -> None:
        if not item.range_info:
            return
        full_name_parts = [item.level1]
        if item.level2:
            full_name_parts.append(item.level2)
        full_name_parts.append(item.name)
        full_name = ", ".join([p for p in full_name_parts if p])

        info_type = str(item.range_info.get("type") or "")
        if info_type == "select":
            options = item.range_info.get("options") or []
            joined = ", ".join(str(opt) for opt in options)
            print(f"[range] Name={full_name} | Optionen={joined}")
            return

        if info_type == "number":
            min_v = item.range_info.get("min")
            max_v = item.range_info.get("max")
            step_v = item.range_info.get("step")
            print(f"[range] Name={full_name} | Min={min_v} | Max={max_v} | Schritt={step_v}")

    def _status_write(self, text: str) -> None:
        clean_text = text.replace("\n", " ").replace("\r", " ")
        pad = max(0, self._status_line_len - len(clean_text))
        sys.stdout.write("\r" + clean_text + (" " * pad))
        sys.stdout.flush()
        self._status_line_len = len(clean_text)

    def _status_clear(self) -> None:
        if self._status_line_len <= 0:
            return
        sys.stdout.write("\r" + (" " * self._status_line_len) + "\r")
        sys.stdout.flush()
        self._status_line_len = 0

    def _print_item(self, item: SubEntry) -> None:
        value_text = f"{item.value}{(' ' + item.unit) if item.unit else ''}"
        full_name_parts = [item.level1]
        if item.level2:
            full_name_parts.append(item.level2)
        full_name_parts.append(item.name)
        full_name = ", ".join([p for p in full_name_parts if p])
        editable = "ja" if item.editor_stack else "nein"
        print(f"[read] Name={full_name} | Bearbeitbar={editable} | Wert={value_text}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="WEM standalone init + poll")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=MIN_REQUEST_GAP_SECONDS,
        help="Minimum seconds between all web requests (default: 4)",
    )
    args = parser.parse_args()

    async with WemInitPoll(min_request_gap_seconds=args.wait_seconds) as app:
        await app.run()


if __name__ == "__main__":
    asyncio.run(main())
