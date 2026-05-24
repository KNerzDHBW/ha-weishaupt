"""WEM web client and HTML parser helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import math
import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_LOGIN_ROUNDS,
    DEFAULT_MAX_HTTP_RETRIES,
)


_LOGGER = logging.getLogger(__name__)


@dataclass
class WriteSpec:
    """Write form metadata used to set a value."""

    action_url: str
    hidden_fields: dict[str, str]
    value_field: str
    scaling_factor: float = 1.0
    select_value_map: dict[str, str] = field(default_factory=dict)


@dataclass
class WemPoint:
    """One discovered WEM parameter."""

    point_id: str
    menu: str
    submenu: str
    name: str
    source_stack: str
    value: Any
    unit: str
    writable: bool
    kind: str
    editor_stack: str | None = None
    options: list[str] = field(default_factory=list)
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    write_spec: WriteSpec | None = None

    @property
    def full_name(self) -> str:
        parts = [self.menu]
        if self.submenu:
            parts.append(self.submenu)
        parts.append(self.name)
        return " - ".join([p for p in parts if p])

    @property
    def submenu_key(self) -> str:
        return f"{self.menu}|{self.submenu}"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _hidden_field_kind(name: str) -> str | None:
    """Classify hidden field names for numeric bounds without false positives."""
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", (name or "").lower()) if tok]
    token_set = set(tokens)
    if "min" in token_set or "minimum" in token_set:
        return "min"
    if "max" in token_set or "maximum" in token_set:
        return "max"
    if (
        "step" in token_set
        or "inc" in token_set
        or "increment" in token_set
        or "schrittweite" in token_set
    ):
        return "step"
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _extract_numeric_token(value: Any) -> float | None:
    """Extract first numeric token from text like '22.0 C' or '22,0'."""
    text = str(value or "").strip()
    if not text:
        return None
    direct = _to_float(text)
    if direct is not None:
        return direct

    match = re.search(r"[+-]?[0-9]+(?:[.,][0-9]+)?", text)
    if match is None:
        return None
    return _to_float(match.group(0))


def _detect_numeric_step(values: list[float]) -> float | None:
    """Return smallest positive distance between sorted numeric values."""
    unique_sorted = sorted(set(values))
    if len(unique_sorted) < 2:
        return None

    diffs: list[float] = []
    for left, right in zip(unique_sorted, unique_sorted[1:]):
        diff = right - left
        if diff > 1e-9:
            diffs.append(diff)

    if not diffs:
        return None
    return min(diffs)


def _resolve_select_post_value(
    requested_value: Any,
    select_value_map: dict[str, str],
) -> str | None:
    """Resolve the raw POST value for a requested select option."""
    requested_text = _normalize(str(requested_value))
    if not requested_text:
        return None

    if requested_text in select_value_map:
        return select_value_map[requested_text]

    # Already a raw POST value.
    for raw_value in select_value_map.values():
        if requested_text == str(raw_value).strip():
            return raw_value

    requested_lower = requested_text.lower()
    for option_text, raw_value in select_value_map.items():
        if option_text.lower() == requested_lower:
            return raw_value

    requested_number = _extract_numeric_token(requested_text)
    if requested_number is not None:
        for option_text, raw_value in select_value_map.items():
            option_number = _extract_numeric_token(option_text)
            if option_number is None:
                continue
            if math.isclose(option_number, requested_number, rel_tol=0.0, abs_tol=1e-9):
                return raw_value

        for raw_value in select_value_map.values():
            raw_number = _extract_numeric_token(raw_value)
            if raw_number is None:
                continue
            if math.isclose(raw_number, requested_number, rel_tol=0.0, abs_tol=1e-9):
                return raw_value

        # Fallback: round to the nearest valid option value.
        numeric_candidates: list[tuple[float, float, str]] = []
        for option_text, raw_value in select_value_map.items():
            option_number = _extract_numeric_token(option_text)
            if option_number is None:
                continue
            numeric_candidates.append((abs(option_number - requested_number), option_number, raw_value))

        if numeric_candidates:
            _, _, nearest_raw_value = min(numeric_candidates, key=lambda item: (item[0], item[1]))
            return nearest_raw_value

    return None


def _split_value_unit(text: str) -> tuple[Any, str]:
    text = _normalize(text)
    if not text:
        return "unknown", ""
    match = re.match(r"^([+-]?[0-9]+(?:[.,][0-9]+)?)\s*(.*)$", text)
    if match:
        number = _to_float(match.group(1))
        if number is not None:
            return number, _normalize(match.group(2))
    return text, ""


def _stack_parts(stack: str) -> list[str]:
    return [part.strip() for part in stack.split(",") if part.strip()]


def _is_direct_child_stack(parent: str, candidate: str) -> bool:
    parent_parts = _stack_parts(parent)
    candidate_parts = _stack_parts(candidate)
    if len(candidate_parts) != len(parent_parts) + 1:
        return False
    return candidate_parts[: len(parent_parts)] == parent_parts


def _stack_tail(stack: str) -> str:
    parts = _stack_parts(stack)
    return parts[-1] if parts else stack


def _extract_stack_from_tag(tag) -> str | None:
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


def _looks_incomplete(html: str) -> bool:
    if not html or len(html.strip()) < 80:
        return True
    lower = html.lower()
    if "loading..." in lower or "bitte warten" in lower or "wird geladen" in lower:
        return len(html) < 1000
    return False


def _is_login_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form")
    if form is None:
        return False
    if form.find("input", {"type": "password"}) is None:
        return False
    action = str(form.get("action") or "").lower()
    body = soup.get_text(" ", strip=True).lower()
    return "login" in action or "einloggen" in body or "anmelden" in body


def _looks_authenticated(html: str) -> bool:
    lower = html.lower()
    return "settings_export.html?stack=" in lower or ("browseobj" in lower and "nav-link" in lower)


def _extract_column_links(html: str, column_index: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    columns = soup.select("main .container .row > div.col-3")
    if len(columns) <= column_index:
        return []

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link in columns[column_index].select("a.nav-link.browseobj"):
        stack = _extract_stack_from_tag(link)
        if not stack or stack in seen:
            continue
        label_node = link.find("h5")
        label = _normalize(label_node.get_text(" ", strip=True) if label_node else "")
        result.append((label or _stack_tail(stack), stack))
        seen.add(stack)
    return result


def _extract_menu_and_submenu_labels(html: str) -> tuple[list[str], list[str]]:
    menu = [name for name, _ in _extract_column_links(html, 0)]
    submenu = [name for name, _ in _extract_column_links(html, 1)]
    return menu, submenu


def looks_like_menu_echo_page(html: str, expected_menu: str, expected_submenu: str) -> bool:
    menu, submenu = _extract_menu_and_submenu_labels(html)
    if not menu or not submenu:
        return False

    menu_set = set(menu)
    submenu_set = set(submenu)
    overlap = len(menu_set & submenu_set)

    if submenu_set and overlap == len(submenu_set) and len(submenu_set) >= 2:
        return True

    if expected_submenu and expected_submenu not in submenu_set:
        if overlap >= max(1, len(submenu_set) // 2):
            return True

    if expected_submenu and expected_menu and expected_menu in submenu_set:
        return True

    return False


class WemWebClient:
    """HTTP client and parser facade for WEM web pages."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        wait_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.wait_seconds = max(0.0, float(wait_seconds))

        self._session: aiohttp.ClientSession | None = None
        self._last_request_ts = 0.0

    async def async_open(self) -> None:
        self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))

    async def async_close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _respect_gap(self) -> None:
        now = time.monotonic()
        wait_for = self.wait_seconds - (now - self._last_request_ts)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        self._last_request_ts = time.monotonic()

    async def _request_text(self, method: str, url: str, **kwargs: Any) -> str:
        if self._session is None:
            raise RuntimeError("Session not initialized")

        last_error: Exception | None = None
        for _ in range(DEFAULT_MAX_HTTP_RETRIES):
            try:
                await self._respect_gap()
                timeout = aiohttp.ClientTimeout(total=DEFAULT_HTTP_TIMEOUT)
                async with self._session.request(
                    method,
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    **kwargs,
                ) as response:
                    text = await response.text()
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status} at {url}")
                    if _looks_incomplete(text):
                        raise RuntimeError(f"Incomplete page at {url}")
                    return text
            except Exception as err:
                last_error = err
                await asyncio.sleep(1)
        raise RuntimeError(f"Request failed for {method} {url}: {last_error}")

    async def _is_authenticated_session(self) -> bool:
        """Return True if current session can access protected pages."""
        if self._session is None:
            return False

        probe_urls = [
            f"{self.base_url}/settings_export.html",
            f"{self.base_url}/home.html",
            f"{self.base_url}/settings_export.html?stack=",
        ]

        for probe_url in probe_urls:
            try:
                await self._respect_gap()
                timeout = aiohttp.ClientTimeout(total=DEFAULT_HTTP_TIMEOUT)
                async with self._session.get(
                    probe_url,
                    timeout=timeout,
                    allow_redirects=True,
                ) as response:
                    text = await response.text()
                    if response.status == 200 and not _is_login_page(text):
                        return True
            except Exception:
                continue

        return False

    async def login(self, progress_callback=None) -> None:
        if self._session is None:
            raise RuntimeError("Session not initialized")

        login_page_url = f"{self.base_url}/index.html"

        for _round in range(1, DEFAULT_LOGIN_ROUNDS + 1):
            await _emit_progress(
                progress_callback,
                {
                    "event": "login_try",
                    "try": _round,
                    "max": DEFAULT_LOGIN_ROUNDS,
                },
            )

            timeout = aiohttp.ClientTimeout(total=DEFAULT_HTTP_TIMEOUT)

            # Warm up cookies/session first.
            try:
                await self._respect_gap()
                async with self._session.get(
                    self.base_url,
                    timeout=timeout,
                    allow_redirects=True,
                ) as response:
                    await response.read()
            except Exception:
                pass

            if await self._is_authenticated_session():
                await _emit_progress(progress_callback, {"event": "login_ok"})
                return

            login_page_html = ""
            form_action = "login.html"
            hidden_fields: dict[str, str] = {}
            username_field_name = "user"
            password_field_name = "pass"
            try:
                await self._respect_gap()
                async with self._session.get(
                    login_page_url,
                    timeout=timeout,
                    allow_redirects=True,
                ) as response:
                    login_page_html = await response.text()
            except Exception:
                login_page_html = ""

            if login_page_html:
                soup = BeautifulSoup(login_page_html, "lxml")
                form = soup.find("form")
                if form is not None:
                    form_action = str(form.get("action") or "login.html")

                    for hidden in form.find_all("input", {"type": "hidden"}):
                        hidden_name = str(hidden.get("name") or "").strip()
                        if hidden_name:
                            hidden_fields[hidden_name] = str(hidden.get("value") or "")

                    for field in form.find_all("input"):
                        field_name = str(field.get("name") or "").strip()
                        if not field_name:
                            continue
                        field_type = str(field.get("type") or "").lower().strip()
                        lname = field_name.lower()

                        if field_type == "password" or lname in {"pass", "password", "pwd"}:
                            password_field_name = field_name
                        elif field_type in {"text", "email", ""} and (
                            "user" in lname or "login" in lname or "name" == lname
                        ):
                            username_field_name = field_name

            payload = dict(hidden_fields)
            payload[username_field_name] = self.username
            payload[password_field_name] = self.password
            # Compatibility aliases for differing firmware/login templates.
            payload.setdefault("user", self.username)
            payload.setdefault("username", self.username)
            payload.setdefault("login", self.username)
            payload.setdefault("pass", self.password)
            payload.setdefault("password", self.password)
            payload.setdefault("pwd", self.password)

            post_targets: list[str] = []
            if form_action.startswith("http"):
                post_targets.append(form_action)
            else:
                post_targets.append(f"{self.base_url}/{form_action.lstrip('/')}")
            for fallback in ("login.html", "index.html", ""):
                fallback_url = f"{self.base_url}/{fallback}".rstrip("/")
                if fallback_url not in post_targets:
                    post_targets.append(fallback_url)

            post_ok = False
            for post_url in post_targets:
                try:
                    await _emit_progress(
                        progress_callback,
                        {"event": "login_post", "target": post_url},
                    )
                    await self._respect_gap()
                    async with self._session.post(
                        post_url,
                        data=payload,
                        timeout=timeout,
                        allow_redirects=True,
                        headers={"Referer": login_page_url, "Origin": self.base_url},
                    ) as response:
                        await response.read()
                    post_ok = True
                    break
                except Exception:
                    continue

            if not post_ok:
                await _emit_progress(
                    progress_callback,
                    {"event": "login_post_failed", "try": _round},
                )
                continue

            await _emit_progress(progress_callback, {"event": "login_verify"})
            if await self._is_authenticated_session():
                await _emit_progress(progress_callback, {"event": "login_ok"})
                return

        await _emit_progress(progress_callback, {"event": "login_failed"})
        raise PermissionError("Login failed")

    async def fetch_settings_root(self) -> str:
        return await self._request_text("GET", f"{self.base_url}/settings_export.html")

    async def fetch_stack(self, stack: str) -> str:
        return await self._request_text("GET", f"{self.base_url}/settings_export.html?stack={stack}")

    async def fetch_stack_reloaded(self, stack: str) -> str:
        return await self._request_text(
            "GET",
            f"{self.base_url}/settings_export.html?stack={stack}&_ts={int(time.time())}",
        )

    async def inspect_editor(self, editor_stack: str) -> tuple[dict[str, Any], WriteSpec | None]:
        html = await self.fetch_stack(editor_stack)
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        if form is None:
            return {}, None

        action = str(form.get("action") or "").strip()
        action_url = action if action.startswith("http") else f"{self.base_url}/{action.lstrip('/')}"
        hidden_fields: dict[str, str] = {}
        for hidden in form.find_all("input", {"type": "hidden"}):
            name = str(hidden.get("name") or "").strip()
            if not name:
                continue
            hidden_fields[name] = str(hidden.get("value") or "")

        select = form.find("select")
        if select is not None:
            options: list[str] = []
            select_value_map: dict[str, str] = {}
            value_field = str(select.get("name") or "value")
            for opt in select.find_all("option"):
                txt = _normalize(opt.get_text(" ", strip=True))
                if txt:
                    options.append(txt)
                    raw_attr = opt.get("value")
                    raw_value = str(raw_attr).strip() if raw_attr is not None else ""
                    # HTML select submits option text if no explicit value attribute is set.
                    select_value_map[txt] = raw_value or txt

            numeric_tokens = [_extract_numeric_token(text) for text in options]
            all_numeric = bool(options) and all(token is not None for token in numeric_tokens)
            if all_numeric:
                numeric_values = [float(token) for token in numeric_tokens if token is not None]
                return {
                    "kind": "number",
                    "min": min(numeric_values) if numeric_values else None,
                    "max": max(numeric_values) if numeric_values else None,
                    "step": _detect_numeric_step(numeric_values),
                    "options": options,
                }, WriteSpec(
                    action_url,
                    hidden_fields,
                    value_field,
                    scaling_factor=1.0,
                    select_value_map=select_value_map,
                )

            return {"kind": "select", "options": options}, WriteSpec(
                action_url,
                hidden_fields,
                value_field,
                scaling_factor=1.0,
                select_value_map=select_value_map,
            )

        number_input = form.find("input", {"type": re.compile(r"^(number|text)$", re.I)})
        if number_input is not None:
            value_field = str(number_input.get("name") or "value")
            current_raw = str(number_input.get("value") or "").strip()
            current_value = _to_float(current_raw)
            min_value = _to_float(number_input.get("min"))
            max_value = _to_float(number_input.get("max"))
            step_value = _to_float(number_input.get("step"))
            scaling_factor = 1.0
            for hidden_name, hidden_value in hidden_fields.items():
                kind = _hidden_field_kind(hidden_name)
                if min_value is None and kind == "min":
                    min_value = _to_float(hidden_value)
                if max_value is None and kind == "max":
                    max_value = _to_float(hidden_value)
                if step_value is None and kind == "step":
                    step_value = _to_float(hidden_value)

            # Guard against false metadata such as maxlength=2 being read as max=2.
            if current_value is not None:
                if max_value is not None and max_value < current_value:
                    max_value = None
                if min_value is not None and min_value > current_value:
                    min_value = None

            # Some WEM editors store values internally scaled by 10 (e.g. 22.0 -> 220)
            # while still exposing decimal step metadata.
            if (
                step_value is not None
                and 0.0 < step_value < 1.0
                and current_value is not None
                and re.search(r"[.,]", current_raw) is None
                and float(current_value).is_integer()
                and abs(current_value) >= 10
            ):
                scaling_factor = 10.0

            return {
                "kind": "number",
                "min": min_value,
                "max": max_value,
                "step": step_value,
            }, WriteSpec(action_url, hidden_fields, value_field, scaling_factor)

        text_input = form.find("input", {"type": re.compile(r"^(text)$", re.I)})
        if text_input is not None:
            value_field = str(text_input.get("name") or "value")
            return {"kind": "text"}, WriteSpec(action_url, hidden_fields, value_field)

        return {}, None

    async def write_point(self, point: WemPoint, new_value: Any) -> None:
        write_spec = point.write_spec
        if write_spec is None:
            if not point.editor_stack:
                raise RuntimeError(f"Point {point.point_id} is not writable")
            details, refreshed_spec = await self.inspect_editor(point.editor_stack)
            if refreshed_spec is None:
                raise RuntimeError(f"No write form found for {point.point_id}")
            write_spec = refreshed_spec
            point.write_spec = refreshed_spec

            refreshed_kind = str(details.get("kind") or "").strip().lower()
            if refreshed_kind == "select":
                point.kind = "select"
                point.options = list(details.get("options", []))
            elif refreshed_kind == "number":
                point.kind = "number"
                point.min_value = details.get("min")
                point.max_value = details.get("max")
                point.step = details.get("step")
            elif refreshed_kind == "text" and point.kind not in {"select", "number"}:
                point.kind = "text"

        data = dict(write_spec.hidden_fields)

        is_select_write = point.kind == "select" or bool(write_spec.select_value_map)

        if is_select_write:
            resolved_raw = _resolve_select_post_value(new_value, write_spec.select_value_map)

            # Refresh editor mapping once if current metadata cannot resolve the requested option.
            if resolved_raw is None and point.editor_stack:
                details, refreshed_spec = await self.inspect_editor(point.editor_stack)
                if refreshed_spec is not None and details.get("kind") == "select":
                    point.write_spec = refreshed_spec
                    write_spec = refreshed_spec
                    point.kind = "select"
                    point.options = list(details.get("options", []))
                resolved_raw = _resolve_select_post_value(new_value, write_spec.select_value_map)

            _LOGGER.debug(
                "Select write for %s: requested=%r resolved_post_value=%r field=%s options=%s",
                point.full_name,
                new_value,
                resolved_raw,
                write_spec.value_field,
                len(write_spec.select_value_map),
            )
            data[write_spec.value_field] = resolved_raw if resolved_raw is not None else str(new_value)
        else:
            numeric_value = None
            try:
                numeric_value = float(str(new_value).replace(",", "."))
            except (TypeError, ValueError):
                numeric_value = None

            if numeric_value is None:
                data[write_spec.value_field] = str(new_value)
            else:
                if write_spec.scaling_factor > 1.0:
                    numeric_value *= write_spec.scaling_factor

                if float(numeric_value).is_integer():
                    data[write_spec.value_field] = str(int(round(numeric_value)))
                else:
                    data[write_spec.value_field] = str(numeric_value)

        await self._request_text(
            "POST",
            write_spec.action_url,
            data=data,
            headers={"Referer": f"{self.base_url}/settings_export.html", "Origin": self.base_url},
        )


def parse_points_from_page(
    html: str,
    menu: str,
    submenu: str,
    source_stack: str,
) -> list[WemPoint]:
    """Parse all points from one settings page."""
    soup = BeautifulSoup(html, "lxml")
    result: list[WemPoint] = []

    for block in soup.select("div.nav-link.browseobj"):
        label_node = block.find("h5")
        if label_node is None:
            continue
        name = _normalize(label_node.get_text(" ", strip=True))
        if not name:
            continue

        inline_form = block.find("form")
        if inline_form is not None:
            point_id = f"{menu}|{submenu}|{name}|{source_stack}"

            # Inline select forms (e.g. System mode page).
            select = inline_form.find("select")
            if select is not None:
                options: list[str] = []
                selected_text = ""
                for opt in select.find_all("option"):
                    text = _normalize(opt.get_text(" ", strip=True))
                    if text:
                        options.append(text)
                    if opt.has_attr("selected") and text:
                        selected_text = text

                action = str(inline_form.get("action") or "").strip()
                action_url = action if action.startswith("http") else f"http://dummy/{action.lstrip('/')}"
                hidden: dict[str, str] = {}
                for hidden_node in inline_form.find_all("input", {"type": "hidden"}):
                    hidden_name = str(hidden_node.get("name") or "").strip()
                    if hidden_name:
                        hidden[hidden_name] = str(hidden_node.get("value") or "")
                value_field = str(select.get("name") or "value")

                result.append(
                    WemPoint(
                        point_id=point_id,
                        menu=menu,
                        submenu=submenu,
                        name=name,
                        source_stack=source_stack,
                        value=selected_text or "unknown",
                        unit="",
                        writable=True,
                        kind="select",
                        editor_stack=source_stack,
                        options=options,
                        write_spec=WriteSpec(
                            action_url,
                            hidden,
                            value_field,
                            scaling_factor=1.0,
                            select_value_map={
                                    _normalize(opt.get_text(" ", strip=True)): (
                                        str(opt.get("value")).strip()
                                        if opt.get("value") is not None and str(opt.get("value")).strip()
                                        else _normalize(opt.get_text(" ", strip=True))
                                    )
                                for opt in select.find_all("option")
                                    if _normalize(opt.get_text(" ", strip=True))
                            },
                        ),
                    )
                )
                continue

        block_text = _normalize(block.get_text(" ", strip=True))
        value_text = block_text[len(name) :].strip() if block_text.startswith(name) else ""
        value, unit = _split_value_unit(value_text)
        editor_stack = _extract_stack_from_tag(block)

        point_id = f"{menu}|{submenu}|{name}|{source_stack}"
        result.append(
            WemPoint(
                point_id=point_id,
                menu=menu,
                submenu=submenu,
                name=name,
                source_stack=source_stack,
                value=value,
                unit=unit,
                writable=editor_stack is not None,
                kind="text",
                editor_stack=editor_stack,
            )
        )

    for link in soup.select("a.nav-link.browseobj"):
        editor_stack = _extract_stack_from_tag(link)
        if not editor_stack or not _is_direct_child_stack(source_stack, editor_stack):
            continue

        label_node = link.find("h5")
        if label_node is None:
            continue
        name = _normalize(label_node.get_text(" ", strip=True))
        if not name:
            continue

        full_text = _normalize(link.get_text(" ", strip=True))
        value_text = full_text[len(name) :].strip() if full_text.startswith(name) else ""
        value, unit = _split_value_unit(value_text)

        point_id = f"{menu}|{submenu}|{name}|{source_stack}"
        if any(point.point_id == point_id for point in result):
            continue

        result.append(
            WemPoint(
                point_id=point_id,
                menu=menu,
                submenu=submenu,
                name=name,
                source_stack=source_stack,
                value=value,
                unit=unit,
                writable=True,
                kind="text",
                editor_stack=editor_stack,
            )
        )

    if not result:
        # Read-only fallback for plain-text rendered values.
        seen: set[str] = set()
        for label_node in soup.find_all("h5"):
            name = _normalize(label_node.get_text(" ", strip=True))
            if not name:
                continue
            container = label_node.find_parent(["div", "li", "tr", "td", "section", "article"])
            if container is None:
                continue
            container_text = _normalize(container.get_text(" ", strip=True))
            if not container_text.startswith(name):
                continue
            value_text = container_text[len(name) :].strip()
            if not value_text:
                continue
            point_id = f"{menu}|{submenu}|{name}|{source_stack}"
            if point_id in seen:
                continue
            seen.add(point_id)
            value, unit = _split_value_unit(value_text)
            result.append(
                WemPoint(
                    point_id=point_id,
                    menu=menu,
                    submenu=submenu,
                    name=name,
                    source_stack=source_stack,
                    value=value,
                    unit=unit,
                    writable=False,
                    kind="sensor",
                )
            )

    return result


async def fetch_root_menus(client: WemWebClient) -> list[tuple[str, str]]:
    """Fetch and return visible main menu entries (name, stack)."""
    root = await client.fetch_settings_root()
    if _is_login_page(root):
        await client.login()
        root = await client.fetch_settings_root()
    return _extract_column_links(root, 0)


async def resolve_menu_submenu_stack(
    client: WemWebClient,
    menu_name: str,
    submenu_name: str,
) -> str | None:
    """Resolve current stack ID from menu/submenu labels."""
    wanted_menu = _normalize(menu_name)
    wanted_submenu = _normalize(submenu_name)

    root_menus = await fetch_root_menus(client)
    menu_stack = None
    for found_menu_name, found_menu_stack in root_menus:
        if _normalize(found_menu_name) == wanted_menu:
            menu_stack = found_menu_stack
            break

    if menu_stack is None:
        return None

    if not wanted_submenu:
        return menu_stack

    menu_html = await client.fetch_stack(menu_stack)
    level2_raw = _extract_column_links(menu_html, 1)
    level2 = [(name, stack) for name, stack in level2_raw if _is_direct_child_stack(menu_stack, stack)]
    for found_submenu_name, found_submenu_stack in level2:
        if _normalize(found_submenu_name) == wanted_submenu:
            return found_submenu_stack

    return None


async def _emit_progress(
    progress_callback,
    payload: dict[str, Any],
) -> None:
    if progress_callback is None:
        return
    maybe = progress_callback(payload)
    if asyncio.iscoroutine(maybe):
        await maybe


async def discover_structure(
    client: WemWebClient,
    selected_menus: set[str] | None = None,
    progress_callback=None,
) -> tuple[dict[str, WemPoint], list[tuple[str, str, str]], list[str], list[str]]:
    """Discover menu structure and all points once at initialization."""
    points: dict[str, WemPoint] = {}
    pages: list[tuple[str, str, str]] = []
    known_menus: list[str] = []
    known_submenus: list[str] = []

    level1 = await fetch_root_menus(client)
    known_menus = [name for name, _ in level1]

    for menu_name, menu_stack in level1:
        if selected_menus is not None and menu_name not in selected_menus:
            continue

        level1_html = await client.fetch_stack(menu_stack)

        level2_raw = _extract_column_links(level1_html, 1)
        level2 = [(name, stack) for name, stack in level2_raw if _is_direct_child_stack(menu_stack, stack)]

        await _emit_progress(
            progress_callback,
            {
                "event": "menu",
                "menu": menu_name,
                "submenus": [name for name, _ in level2],
            },
        )

        if not level2:
            pages.append((menu_name, "", menu_stack))
            for point in parse_points_from_page(level1_html, menu_name, "", menu_stack):
                points[point.point_id] = point
            await _emit_progress(
                progress_callback,
                {"event": "submenu_done", "menu": menu_name, "submenu": ""},
            )
            continue

        for submenu_name, submenu_stack in level2:
            pages.append((menu_name, submenu_name, submenu_stack))
            known_submenus.append(f"{menu_name}|{submenu_name}")
            page_html = await client.fetch_stack(submenu_stack)
            for point in parse_points_from_page(page_html, menu_name, submenu_name, submenu_stack):
                points[point.point_id] = point
            await _emit_progress(
                progress_callback,
                {"event": "submenu_done", "menu": menu_name, "submenu": submenu_name},
            )

    # One-time editor inspection for writable points to classify kind/range/options.
    await _emit_progress(
        progress_callback,
        {
            "event": "finalize_start",
            "total": len([p for p in points.values() if p.writable and p.editor_stack]),
        },
    )

    inspected = 0
    for point in points.values():
        if not point.writable or not point.editor_stack:
            continue
        details, write_spec = await client.inspect_editor(point.editor_stack)
        if write_spec is not None:
            # Replace dummy base URL used for inline forms with real base URL.
            if write_spec.action_url.startswith("http://dummy/"):
                write_spec.action_url = f"{client.base_url}/{write_spec.action_url.removeprefix('http://dummy/')}"
            point.write_spec = write_spec

        kind = details.get("kind")
        if kind == "select":
            point.kind = "select"
            point.options = list(details.get("options", []))
        elif kind == "number":
            point.kind = "number"
            point.min_value = details.get("min")
            point.max_value = details.get("max")
            point.step = details.get("step")
        elif point.writable:
            point.kind = "text"

        inspected += 1
        await _emit_progress(
            progress_callback,
            {
                "event": "finalize_step",
                "done": inspected,
                "item": point.full_name,
            },
        )

    await _emit_progress(progress_callback, {"event": "finalize_done"})

    return points, pages, known_menus, sorted(set(known_submenus))


def normalize_for_compare(value: Any) -> str:
    """Normalize runtime values for write verification."""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isfinite(value):
            return f"{value:.6f}".rstrip("0").rstrip(".")
        return str(value)

    numeric_token = _extract_numeric_token(value)
    if numeric_token is not None and math.isfinite(numeric_token):
        return f"{numeric_token:.6f}".rstrip("0").rstrip(".")

    return _normalize(str(value))
