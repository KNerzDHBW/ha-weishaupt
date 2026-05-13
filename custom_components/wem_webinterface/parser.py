"""
HTML parser for WEM settings_export.html pages.

Handles three cases:
  - Writable numeric parameter  (form with number input, hidden min/max/step)
  - Writable select parameter   (form with <select> element)
  - Read-only table of values   (table rows with name/value pairs)
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Any, Tuple, Dict

from bs4 import BeautifulSoup, Tag

_LOGGER = logging.getLogger(__name__)

# Common unit strings (order matters – longer first to avoid partial matches)
_KNOWN_UNITS = [
    "°C", "°F", "K",
    "kWh", "kW", "Wh", "W",
    "m³/h", "l/min", "l/h",
    "bar", "hPa", "Pa",
    "U/min", "rpm",
    "Hz", "V", "A",
    "%",
]


@dataclass
class ParsedParameter:
    """One parameter discovered from a settings_export.html page."""
    param_id: str           # unique within this page (slugified name)
    name: str               # human-readable label
    current_value: Any      # current reading (float or str)
    param_type: str         # "number" | "select" | "readonly"
    unit: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    step: Optional[float] = None
    options: Optional[List[str]] = None
    form_field_name: Optional[str] = None  # HTML field name for POST
    write_action: Optional[str] = None
    write_fields: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_settings_page(html: str, stack: str) -> Optional[List[ParsedParameter]]:
    """
    Parse a settings_export.html response.

    Returns:
        List[ParsedParameter]  – on success (may be empty list if nothing found)
        None                   – page is incomplete / not yet loaded
    """
    if not html or len(html.strip()) < 50:
        _LOGGER.debug("Page too short (%d chars) – incomplete", len(html) if html else 0)
        return None

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:
        _LOGGER.error("BeautifulSoup parse error: %s", exc)
        return None

    if is_login_page(soup):
        _LOGGER.debug("Received login page instead of settings page for stack %s", stack[:30])
        return None

    if _is_incomplete(soup):
        _LOGGER.debug("Page marked as incomplete for stack %s", stack[:30])
        return None

    result = _parse_wem_layout(soup, stack)
    if result is not None:
        return result

    # ---- strategy 1: form with input/select  (writable) ------------------
    result = _try_form(soup, stack)
    if result is not None:
        return result

    # ---- strategy 2: table rows  (read-only multiple values) -------------
    result = _try_table(soup, stack)
    if result is not None:
        return result

    # ---- strategy 3: definition lists / labelled spans -------------------
    result = _try_dl(soup, stack)
    if result is not None:
        return result

    # ---- strategy 4: generic key-value search ----------------------------
    result = _try_generic_kv(soup, stack)
    if result is not None:
        return result

    _LOGGER.warning(
        "No parameters found for stack %s\nHTML preview: %s",
        stack[:40],
        html[:600],
    )
    # Return empty list so callers know the page loaded but contained nothing
    return []


def _parse_wem_layout(soup: BeautifulSoup, stack: str) -> Optional[List[ParsedParameter]]:
    """Parse the actual WEM dashboard layout used by settings_export.html."""
    content_columns = soup.select("main.col-md-9 > div.container.mx-0 > div.row > div.col-3")
    if not content_columns:
        return None

    active_labels = _active_labels(soup)
    content_column = content_columns[-1]

    form = content_column.find("form")
    if form and form.find("select"):
        parsed = _parse_wem_writable_form(form, active_labels, stack)
        return [parsed] if parsed else []

    readonly_blocks = [
        block for block in content_column.select("div.nav-link.browseobj")
        if not block.find("form")
    ]
    if readonly_blocks:
        return _parse_wem_readonly_blocks(readonly_blocks, active_labels, stack)

    return []


def _active_labels(soup: BeautifulSoup) -> List[str]:
    labels: List[str] = []
    for node in soup.select(".nav-link.browseobj.activeobj h5"):
        text = _normalize_text(node.get_text(" ", strip=True))
        if text:
            labels.append(text)
    return labels


def _parse_wem_writable_form(form: Tag, active_labels: List[str], stack: str) -> Optional[ParsedParameter]:
    select = form.find("select")
    if select is None:
        return None

    option_texts = [_normalize_text(opt.get_text(" ", strip=True)) for opt in select.find_all("option")]
    numeric_values = [_to_float(text) for text in option_texts]
    numeric_mode = bool(option_texts) and all(value is not None for value in numeric_values)

    selected_option = select.find("option", selected=True)
    if selected_option is None:
        selected_option = select.find("option")

    current_text = _normalize_text(selected_option.get_text(" ", strip=True)) if selected_option else ""
    current_value: Any = _to_float(current_text) if numeric_mode else current_text

    hidden_fields: Dict[str, str] = {}
    for hidden in form.find_all("input", {"type": "hidden"}):
        name = hidden.get("name")
        if name:
            hidden_fields[name] = hidden.get("value", "")

    label = _leaf_label(form)
    name_parts = list(active_labels)
    if label and (not name_parts or name_parts[-1] != label):
        name_parts.append(label)
    if not name_parts and label:
        name_parts = [label]

    if numeric_mode:
        numeric_option_values = [value for value in numeric_values if value is not None]
        numeric_option_values = sorted(set(numeric_option_values))
        step = _detect_step(numeric_option_values)
        unit = _infer_unit(form, name_parts)
        if not unit and any("temperatur" in part.lower() for part in name_parts):
            unit = "°C"
        return ParsedParameter(
            param_id=_slugify(" ,".join(name_parts)).replace(" ", ""),
            name=", ".join(name_parts) if name_parts else label or stack,
            current_value=current_value,
            param_type="number",
            unit=unit,
            min_value=min(numeric_option_values) if numeric_option_values else None,
            max_value=max(numeric_option_values) if numeric_option_values else None,
            step=step,
            form_field_name=select.get("name") or "value",
            write_action=form.get("action") or "pro_save.html",
            write_fields=hidden_fields,
        )

    return ParsedParameter(
        param_id=_slugify(" ,".join(name_parts)).replace(" ", ""),
        name=", ".join(name_parts) if name_parts else label or stack,
        current_value=current_value,
        param_type="select",
        options=option_texts,
        form_field_name=select.get("name") or "value",
        write_action=form.get("action") or "pro_save.html",
        write_fields=hidden_fields,
    )


def _parse_wem_readonly_blocks(blocks: List[Tag], active_labels: List[str], stack: str) -> List[ParsedParameter]:
    parameters: List[ParsedParameter] = []
    prefix = active_labels[-1] if active_labels else ""
    seen_ids: set[str] = set()

    for block in blocks:
        label_node = block.find("h5")
        if not label_node:
            continue
        label = _normalize_text(label_node.get_text(" ", strip=True))
        if not label:
            continue

        block_text = _normalize_text(block.get_text(" ", strip=True))
        value_text = block_text[len(label):].strip() if block_text.startswith(label) else block_text.replace(label, "", 1).strip()
        value, unit = _split_value_unit(value_text)
        name = f"{prefix}, {label}" if prefix else label

        param_id = _slugify(name)
        suffix = 1
        while param_id in seen_ids:
            param_id = f"{_slugify(name)}_{suffix}"
            suffix += 1
        seen_ids.add(param_id)

        parameters.append(
            ParsedParameter(
                param_id=param_id,
                name=name,
                current_value=value,
                param_type="readonly",
                unit=unit,
            )
        )

    return parameters


def _leaf_label(form: Tag) -> str:
    parent = form.parent
    while parent is not None:
        label = parent.find("h5", recursive=False)
        if label:
            return _normalize_text(label.get_text(" ", strip=True))
        parent = parent.parent
    return ""


def _detect_step(values: List[float]) -> Optional[float]:
    """Detect step size from sorted option values.
    
    If all differences are multiples of 10, and individual values look like
    they may be scaled by 10 (e.g., 205 instead of 20.5), return 0.1 instead of 1.
    """
    if len(values) < 2:
        return None
    diffs = [round(values[i + 1] - values[i], 10) for i in range(len(values) - 1) if values[i + 1] > values[i]]
    if not diffs:
        return None
    
    base_step = min(diffs)
    # Detect scaled values: if smallest diff is 10 and values are all divisible by 10,
    # likely the device stores 20.5 as 205, so report step as 0.1 not 1
    if base_step >= 10:
        if all(v % 10 == 0 for v in values if v > 0):
            return base_step / 10.0
    
    return base_step


def _infer_unit(node: Tag, labels: List[str]) -> str:
    text = _normalize_text(node.get_text(" ", strip=True))
    for unit in _KNOWN_UNITS:
        if unit and unit in text:
            return unit
    label_text = " ".join(labels).lower()
    if any(token in label_text for token in ("temperatur", "solltemperatur", "raum", "vorlauf", "rücklauf")):
        return "°C"
    return ""


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# ---------------------------------------------------------------------------
# Completeness check
# ---------------------------------------------------------------------------

def _is_incomplete(soup: BeautifulSoup) -> bool:
    """Return True if the page hasn't finished loading."""
    body = soup.find("body")
    if not body:
        return True
    body_text = body.get_text(strip=True)
    if len(body_text) < 5:
        return True
    loading_phrases = ["loading...", "bitte warten", "please wait", "lade...", "wird geladen"]
    lower = body_text.lower()
    for phrase in loading_phrases:
        if phrase in lower and len(body_text) < 200:
            return True
    return False


def is_login_page(soup: BeautifulSoup) -> bool:
    """Return True when the device serves the login form instead of data."""
    form = soup.find("form")
    if not form:
        return False

    action = (form.get("action") or "").strip().lower()
    has_password_field = form.find("input", {"type": "password"}) is not None
    heading = soup.find(["h1", "h2", "title"])
    heading_text = heading.get_text(" ", strip=True).lower() if heading else ""
    body_text = soup.get_text(" ", strip=True).lower()

    return (
        action.endswith("/login.html")
        and has_password_field
        and ("bitte einloggen" in body_text or "wem lokal" in heading_text)
    )


# ---------------------------------------------------------------------------
# Strategy 1 – Form-based (writable parameter)
# ---------------------------------------------------------------------------

def _try_form(soup: BeautifulSoup, stack: str) -> Optional[List[ParsedParameter]]:
    forms = soup.find_all("form")
    if not forms:
        return None

    parameters: List[ParsedParameter] = []
    for form in forms:
        p = _parse_single_form(form, soup, stack)
        if p:
            parameters.append(p)

    return parameters if parameters else None


def _parse_single_form(form: Tag, soup: BeautifulSoup, stack: str) -> Optional[ParsedParameter]:
    name = _extract_name(soup)
    if not name:
        return None

    param_id = _slugify(name)
    scaling_factor = 1.0

    # --- select element → string options ---
    sel_elem = form.find("select")
    if sel_elem:
        opts = [o.get_text(strip=True) for o in sel_elem.find_all("option") if o.get_text(strip=True)]
        selected = sel_elem.find("option", selected=True)
        current = selected.get_text(strip=True) if selected else (opts[0] if opts else None)
        if opts:
            return ParsedParameter(
                param_id=param_id,
                name=name,
                current_value=current,
                param_type="select",
                options=opts,
                form_field_name=sel_elem.get("name") or "value",
            )

    # --- number / text input ---
    # Collect all relevant inputs
    value_input = None
    min_val = max_val = step_val = None

    for inp in form.find_all("input"):
        itype = (inp.get("type") or "text").lower()
        iname = (inp.get("name") or "").lower()

        if itype == "hidden":
            if re.search(r"\bmin\b", iname):
                min_val = _to_float(inp.get("value"))
            elif re.search(r"\bmax\b", iname):
                max_val = _to_float(inp.get("value"))
            elif re.search(r"\bstep\b|\binc\b|\bschrittweite\b", iname):
                step_val = _to_float(inp.get("value"))
        elif itype in ("number", "text") and value_input is None:
            value_input = inp

    if value_input is None:
        return None

    raw_val = value_input.get("value", "")
    raw_float = _to_float(raw_val)
    
    # Prefer explicit min/max/step from input attributes if not found in hiddens
    if min_val is None:
        min_val = _to_float(value_input.get("min"))
    if max_val is None:
        max_val = _to_float(value_input.get("max"))
    if step_val is None:
        step_val = _to_float(value_input.get("step"))
    
    # Detect if values are scaled by 10 (e.g., WEM stores 20.5°C as 205)
    # Heuristic: step is ~0.1 but raw value is >= 10
    if step_val is not None and raw_float is not None:
        if 0.05 < step_val < 1.0 and abs(step_val - 0.1) < 0.05:
            if raw_float >= 10:
                scaling_factor = 10.0
                _LOGGER.debug("Detected 10x scaling for %s (step=%.2f, raw_val=%.1f)", name, step_val, raw_float)

    current_value = _to_float(raw_val)
    if current_value is None:
        current_value = raw_val
    elif scaling_factor == 10.0 and current_value is not None:
        current_value = current_value / scaling_factor
        if min_val is not None:
            min_val = min_val / scaling_factor
        if max_val is not None:
            max_val = max_val / scaling_factor
        if step_val is not None:
            step_val = step_val / scaling_factor

    unit = _extract_unit(soup)

    param = ParsedParameter(
        param_id=param_id,
        name=name,
        current_value=current_value,
        param_type="number",
        unit=unit,
        min_value=min_val,
        max_value=max_val,
        step=step_val if step_val is not None else 1.0,
        form_field_name=value_input.get("name") or "value",
    )
    
    # Store scaling factor as marker for later write operations
    if scaling_factor == 10.0:
        param.write_fields = {"__scaling_factor__": "10"}
    
    return param


# ---------------------------------------------------------------------------
# Strategy 2 – Table rows (read-only multi-value pages)
# ---------------------------------------------------------------------------

def _try_table(soup: BeautifulSoup, stack: str) -> Optional[List[ParsedParameter]]:
    parameters: List[ParsedParameter] = []
    seen_ids: set = set()

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            name_text = cells[0].get_text(strip=True)
            val_text  = cells[1].get_text(strip=True)

            if not name_text or name_text.lower() in {"name", "wert", "value", "einheit", "bezeichnung"}:
                continue

            unit = cells[2].get_text(strip=True) if len(cells) >= 3 else ""
            value, embedded_unit = _split_value_unit(val_text)
            if not unit:
                unit = embedded_unit

            pid = _slugify(name_text)
            # De-duplicate within the same page
            base_pid = pid
            suffix = 1
            while pid in seen_ids:
                pid = f"{base_pid}_{suffix}"
                suffix += 1
            seen_ids.add(pid)

            parameters.append(ParsedParameter(
                param_id=pid,
                name=name_text,
                current_value=value,
                param_type="readonly",
                unit=unit,
            ))

    return parameters if parameters else None


# ---------------------------------------------------------------------------
# Strategy 3 – Definition list <dl>/<dt>/<dd>
# ---------------------------------------------------------------------------

def _try_dl(soup: BeautifulSoup, stack: str) -> Optional[List[ParsedParameter]]:
    parameters: List[ParsedParameter] = []
    dts = soup.find_all("dt")
    dds = soup.find_all("dd")

    for dt, dd in zip(dts, dds):
        name = dt.get_text(strip=True)
        val_text = dd.get_text(strip=True)
        if not name:
            continue
        value, unit = _split_value_unit(val_text)
        parameters.append(ParsedParameter(
            param_id=_slugify(name),
            name=name,
            current_value=value,
            param_type="readonly",
            unit=unit,
        ))

    return parameters if parameters else None


# ---------------------------------------------------------------------------
# Strategy 4 – Generic labelled divs / spans
# ---------------------------------------------------------------------------

def _try_generic_kv(soup: BeautifulSoup, stack: str) -> Optional[List[ParsedParameter]]:
    """Last-resort: look for elements with CSS classes hinting at label/value pairs."""
    parameters: List[ParsedParameter] = []

    label_pat = re.compile(r"label|name|title|bezeichnung", re.I)
    value_pat = re.compile(r"value|val|reading|messwert|wert", re.I)

    value_elems = soup.find_all(class_=value_pat)
    for velem in value_elems:
        # Look for a sibling or nearby label
        label_elem = velem.find_previous(class_=label_pat)
        if not label_elem:
            label_elem = velem.find_previous_sibling()
        if not label_elem:
            continue

        name = label_elem.get_text(strip=True)
        val_text = velem.get_text(strip=True)
        if not name or not val_text:
            continue

        value, unit = _split_value_unit(val_text)
        parameters.append(ParsedParameter(
            param_id=_slugify(name),
            name=name,
            current_value=value,
            param_type="readonly",
            unit=unit,
        ))

    return parameters if parameters else None


# ---------------------------------------------------------------------------
# Helpers for name / unit extraction
# ---------------------------------------------------------------------------

def _extract_name(soup: BeautifulSoup) -> Optional[str]:
    """Extract the main parameter name from the page."""
    # Headings first
    for tag in ("h1", "h2", "h3", "h4"):
        elem = soup.find(tag)
        if elem:
            t = elem.get_text(strip=True)
            if t and len(t) > 2:
                return t

    # <title>
    title = soup.find("title")
    if title:
        t = title.get_text(strip=True)
        if t and t.lower() not in ("settings", "einstellungen", "wem"):
            return t

    # <label> elements
    for label in soup.find_all("label"):
        t = label.get_text(strip=True)
        if t and len(t) > 2:
            return t

    # Divs/spans with suggestive class
    for cls in ("title", "name", "parameter-name", "param-name", "heading", "bezeichnung"):
        elem = soup.find(class_=cls)
        if elem:
            t = elem.get_text(strip=True)
            if t and len(t) > 2:
                return t

    # First non-trivial <td>
    for td in soup.find_all("td"):
        t = td.get_text(strip=True)
        if t and len(t) > 2 and not t.replace(".", "").replace(",", "").isnumeric():
            return t

    return None


def _extract_unit(soup: BeautifulSoup) -> str:
    """Try to find a unit string on the page."""
    # Explicit unit element
    for cls in ("unit", "einheit", "dimension", "uom"):
        elem = soup.find(class_=cls)
        if elem:
            return elem.get_text(strip=True)

    # data-unit attribute on an input
    for inp in soup.find_all("input"):
        u = inp.get("data-unit") or inp.get("unit", "")
        if u:
            return u

    return ""


# ---------------------------------------------------------------------------
# Value / unit splitting utilities
# ---------------------------------------------------------------------------

def _split_value_unit(text: str) -> Tuple[Any, str]:
    """
    Split "23.5 °C" → (23.5, "°C"), "Ein" → ("Ein", ""), etc.
    """
    if not text:
        return None, ""

    for unit in _KNOWN_UNITS:
        if text.endswith(unit):
            val_str = text[: -len(unit)].strip()
            return _to_float_or_str(val_str), unit

    # Try splitting at last whitespace
    parts = text.rsplit(None, 1)
    if len(parts) == 2:
        v = _to_float(parts[0])
        if v is not None:
            return v, parts[1]

    return _to_float_or_str(text), ""


def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _to_float_or_str(text: str) -> Any:
    v = _to_float(text)
    return v if v is not None else text.strip()


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------

_UMLAUT_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def _slugify(text: str) -> str:
    text = text.lower().translate(_UMLAUT_MAP)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text[:64] if text else "unknown"
