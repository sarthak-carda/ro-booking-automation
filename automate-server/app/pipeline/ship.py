"""Stage 4 — Ship.

Ports src/pipeline/ship.js. Writes a passing script into the registry, keyed
by the vendor URL's host (matches how the extension resolves scripts:
`fetchAutofillScript` passes the vendor link as a query param — see
app/routes/scripts.py for the lookup side). A human has reviewed the script
before this is called; this module does not gate on anything itself beyond
"Stage 3 passed."
"""
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from app.db import script_registry

try:
    from app.lib.vendor_url import host_for
except ImportError:  # app/lib/vendor_url.py not landed yet (another agent's slice) — stub
    # locally with the same logic so this module isn't blocked; reconciled at integration.
    def host_for(link: str) -> str:
        """Normalizes a vendor link to the registry key: its host."""
        try:
            parsed = urlparse(link)
            if not parsed.netloc:
                raise ValueError("no netloc")
            return parsed.netloc.lower()
        except Exception:
            return str(link).lower()


def _iso_now() -> str:
    """Matches JS `new Date().toISOString()` formatting: millisecond precision + "Z"."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def ship_script(vendor_id, portal_url: str, script: str, audit_report: dict, reviewed_by: Optional[str] = None) -> dict:
    host = host_for(portal_url)
    existing = script_registry.get(host)
    version = (existing or {}).get("version")
    version = version if version is not None else 0
    return script_registry.set(host, {
        "vendorId": vendor_id,
        "portalUrl": portal_url,
        "host": host,
        "source": script,
        "version": version + 1,
        "shippedAt": _iso_now(),
        "auditReport": audit_report,
        "reviewedBy": reviewed_by,
    })
