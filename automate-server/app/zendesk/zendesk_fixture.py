"""Mirrors src/zendesk/zendeskFixture.js: Zendesk manual-ticket-volume pull —
stubbed with a fixture this pass (no live Zendesk access available). Mimics
the real data shape so the threshold-check logic in
app/threshold/check_threshold.py is real and testable now, and swaps for a
live pull later without changing callers.

Real shape (per the plan, §9): vendor, tour, manual-ticket count, date — per
vendor AND per tour, since some vendors are automatable for some tours and
not others.

Expected import: `from app.config import config` (app/config.py, already
written by another agent as of this pass — importable now).
"""
import json
import os

from app.config import config


async def pull_manual_ticket_volume() -> list[dict]:
    """Returns [{vendorId, tourId, vendorName, manualTicketCount, windowStart, windowEnd}, ...]."""
    if not config.zendesk_stub:
        raise RuntimeError(
            "Real Zendesk integration is not wired up. Set ZENDESK_STUB=true or implement a real pull here."
        )
    file_path = os.path.join(config.fixtures_dir, "zendesk-tickets.fixture.json")
    with open(file_path, "r", encoding="utf8") as f:
        return json.load(f)
