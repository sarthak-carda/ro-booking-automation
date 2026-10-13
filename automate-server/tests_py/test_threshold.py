"""Ports tests/threshold.test.js.

NOTE on process isolation: the Node test runner (`node --test`) runs each
test *file* in its own process by default, so each file's
`process.env.AUTOMATE_DATA_DIR = ...` set before its dynamic `import()` is
isolated from every other file. pytest runs all files in one process, so
this env-var-before-import trick only gives real isolation when this file is
run on its own (`.venv/bin/pytest tests_py/test_threshold.py`) — exactly as
instructed. Running the whole tests_py/ directory in a single pytest
invocation will NOT re-isolate AUTOMATE_DATA_DIR per file, because
`app.config`/`app.db` are only ever imported (and their module-level state
built) once per process; whichever test file's import runs first "wins" the
data dir for the whole session. Flagging this for the orchestrating session:
CI should invoke pytest once per tests_py/test_*.py file (or use
pytest-forked / a subprocess-per-file runner) to match Node's behavior.
"""
import os
import shutil
import tempfile

import pytest

_DATA_DIR = tempfile.mkdtemp(prefix="automate-server-test-threshold-")
os.environ["AUTOMATE_DATA_DIR"] = f"{_DATA_DIR}/"
os.environ["ZENDESK_STUB"] = "true"
os.environ["AUTOMATION_THRESHOLD"] = "30"

from app.threshold.check_threshold import check_eligible_vendors  # noqa: E402
from app.db import script_registry  # noqa: E402
from app.config import config  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _cleanup_data_dir():
    yield
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


@pytest.mark.asyncio
async def test_threshold_flags_vendor_tour_pairs_at_or_above_threshold():
    """Threshold job flags vendor/tour pairs at or above the configured threshold, per tour."""
    eligible = await check_eligible_vendors()

    # From fixtures/zendesk-tickets.fixture.json: threshold is 30. Panorama Langkawi
    # (tour 71040) sits at 29 -- deliberately just under, to prove the boundary is real.
    eligible_tour_ids = sorted(e["tourId"] for e in eligible)
    assert eligible_tour_ids == [71001, 71010, 71030, 71050]

    # TOSC Manual has two tours; only the one at/above threshold (42) should surface, not the one at 11.
    tosc_tours = [e["tourId"] for e in eligible if e["vendorId"] == 4105]
    assert tosc_tours == [71001]

    for row in eligible:
        assert row["manualTicketCount"] >= row["threshold"], (
            f"{row['vendorName']}/{row['tourId']} should be >= threshold"
        )


@pytest.mark.asyncio
async def test_already_shipped_vendor_is_flagged_as_already_covered():
    """A vendor already shipped is flagged as already covered, not re-queued blind."""
    script_registry.set(
        "prado.example.com",
        {"vendorId": 2078, "portalUrl": "https://prado.example.com", "source": "// shipped"},
    )
    eligible = await check_eligible_vendors({2078: {"portalUrl": "https://prado.example.com"}})
    prado = next(e for e in eligible if e["vendorId"] == 2078)
    assert prado["alreadyShipped"] is True


@pytest.mark.asyncio
async def test_raising_threshold_changes_eligibility_without_code_change():
    """Raising the threshold changes eligibility without a code change."""
    original = config.automation_threshold
    config.automation_threshold = 100
    try:
        eligible = await check_eligible_vendors()
        assert len(eligible) == 0, "nothing should qualify once the threshold is raised above all fixture counts"
    finally:
        config.automation_threshold = original
