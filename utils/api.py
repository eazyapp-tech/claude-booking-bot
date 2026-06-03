from core.log import get_logger

logger = get_logger("utils.api")


class RentokAPIError(Exception):
    """Raised when Rentok API returns a success HTTP status but an error payload."""
    pass


def user_error(action: str, exc: object = "", *, logger=None) -> str:
    """Clean, user-facing error message that never leaks internals.

    The real exception (which can carry URLs, HTTP status codes, tracebacks, or
    raw Python error text) is logged when a logger is supplied, but never put in
    the returned string — the user only ever sees a friendly apology.

    `action` is a short verb phrase describing what failed, e.g. "cancel your
    booking" → "Sorry, I couldn't cancel your booking right now ...".
    """
    if logger is not None and exc:
        logger.error("%s failed: %s", action, exc)
    return (
        f"Sorry, I couldn't {action} right now due to a temporary issue. "
        "Please try again in a moment."
    )


def check_rentok_response(data: dict, context: str = "") -> dict:
    """Validate Rentok API response. Raises RentokAPIError if payload indicates failure."""
    status = data.get("status")
    if isinstance(status, int) and status >= 400:
        msg = data.get("message", "Unknown error")
        logger.warning("Rentok API error [%s]: status=%s msg=%s", context, status, msg)
        raise RentokAPIError(f"API error ({status}): {msg}")
    if isinstance(status, str) and status.lower() == "error":
        msg = data.get("message", "Unknown error")
        logger.warning("Rentok API error [%s]: %s", context, msg)
        raise RentokAPIError(f"API error: {msg}")
    return data


# ── RentOK field normalisers ──────────────────────────────────────────────────
# Centralised here so every tool that touches the API uses the same logic.
# Use these at *write* time (search.py → Redis cache) AND at *read* time
# (property_details, compare) — but only one canonical copy exists.

def parse_amenities(val, fallback: str = "") -> str:
    """Convert a RentOK amenities value to a human-readable comma-separated string.

    Handles all three shapes the API produces:
    - dict of categories:  {"furniture": [{"name": "WiFi", "is_selected": True}, ...], ...}
      → names where is_selected=True (treats absent flag as True)
    - list of dicts:       [{"name": "WiFi"}, ...]  → 'name' field
    - list of strings:     ["WiFi", "AC"]            → joined directly
    - plain string:        returned as-is
    """
    if isinstance(val, dict):
        names = []
        for items in val.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    if item.get("is_selected", True):
                        name = item.get("name") or item.get("amenity_name", "")
                        if name:
                            names.append(name)
                elif item:
                    names.append(str(item))
        return ", ".join(names) or fallback

    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                name = item.get("name") or item.get("amenity_name", "")
                if name:
                    parts.append(name)
            elif item:
                parts.append(str(item))
        return ", ".join(parts) or fallback

    return val or fallback


def parse_sharing_types(val, fallback: str = "") -> str:
    """Convert a RentOK sharing_types value to a human-readable string.

    Handles:
    - list of dicts: [{"sharing_type": "Double", "is_enabled": True, "rent": 5000}, ...]
      → "Double (₹5000/mo)", disabled entries skipped
    - list of strings: ["Single", "Double"]  → joined directly
    - plain string: returned as-is
    """
    if not val:
        return fallback
    if isinstance(val, str):
        return val or fallback
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                stype = item.get("sharing_type") or item.get("type") or item.get("name", "")
                enabled = item.get("is_enabled", item.get("enabled", True))
                if not stype or not enabled:
                    continue
                rent = (item.get("rent") or item.get("starting_rent")
                        or item.get("rent_starts_from", ""))
                parts.append(f"{stype} (₹{rent}/mo)" if rent else stype)
            elif item:
                parts.append(str(item))
        return ", ".join(parts) or fallback
    return str(val) or fallback


def parse_sharing_types_structured(val) -> list:
    """Structured form of parse_sharing_types for the detail sheet's 'Choose sharing'
    section. The frontend (eazypg-chat property-sheet.js _sharingOptions) reads an
    ARRAY of {label, price} — a flat display string (parse_sharing_types) fails its
    Array.isArray check and renders nothing.

    Accepts the RentOK list-of-dicts shape (same fields parse_sharing_types reads);
    skips disabled entries. Returns [] for empty / string / unknown shapes so the
    sheet section stays hidden (graceful degradation, no empty shell).
    """
    if not val or not isinstance(val, list):
        return []
    out = []
    for item in val:
        if isinstance(item, dict):
            stype = item.get("sharing_type") or item.get("type") or item.get("name", "")
            enabled = item.get("is_enabled", item.get("enabled", True))
            if not stype or not enabled:
                continue
            rent = (item.get("rent") or item.get("starting_rent")
                    or item.get("rent_starts_from", ""))
            out.append({"label": str(stype), "price": f"₹{rent}/mo" if rent else ""})
        elif item:
            out.append({"label": str(item), "price": ""})
    return out
