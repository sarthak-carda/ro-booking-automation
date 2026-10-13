"""Mirrors src/threshold/checkThreshold.js: threshold-based vendor/tour
eligibility (plan §9).

Pulls manual-ticket volume per vendor+tour, compares against a configurable
threshold (env AUTOMATION_THRESHOLD, not hardcoded), and for anything that
crosses it and isn't already shipped, queues it as "eligible" — a real caller
(a cron, or the `/api/eligible-vendors` "trigger now" action) then feeds that
straight into the pipeline runner (same pipeline as §5, not a separate code
path).

Expected imports (all already written by other agents as of this pass, so
importable now — no stub fallback needed):
  - app.config.config              (app/config.py)
  - app.db.script_registry         (app/db.py)
  - app.lib.vendor_url.host_for    (app/lib/vendor_url.py)
  - app.zendesk.zendesk_fixture.pull_manual_ticket_volume (this agent's own slice)
"""
from typing import Any, Optional

from app.config import config
from app.db import script_registry
from app.lib.vendor_url import host_for
from app.zendesk.zendesk_fixture import pull_manual_ticket_volume


async def check_eligible_vendors(vendor_portal_by_id: Optional[dict] = None) -> list[dict]:
    """vendor_portal_by_id: dict of {vendorId: {"portalUrl": str}} (or any
    Mapping-like object with .get), to resolve whether a vendor/tour is
    already shipped. Mirrors the JS `Map<number, {portalUrl}>` default arg.

    Returns [{vendorId, tourId, vendorName, manualTicketCount, windowStart,
    windowEnd, threshold, alreadyShipped}, ...] for rows whose
    manualTicketCount >= config.automation_threshold.
    """
    if vendor_portal_by_id is None:
        vendor_portal_by_id = {}

    rows = await pull_manual_ticket_volume()
    threshold = config.automation_threshold

    result = []
    for row in rows:
        if row["manualTicketCount"] < threshold:
            continue
        entry = vendor_portal_by_id.get(row["vendorId"])
        portal_url = entry.get("portalUrl") if entry else None
        already_shipped = bool(script_registry.get(host_for(portal_url))) if portal_url else False
        result.append({**row, "threshold": threshold, "alreadyShipped": already_shipped})
    return result
