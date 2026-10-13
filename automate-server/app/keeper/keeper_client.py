"""Mirrors src/keeper/keeperClient.js: Keeper credential client — interface
only for this pass.

Real Keeper access (CLI/API) was not wired up this pass (no credentials
available). This module is the seam: swap the stub branch below for a real
Keeper lookup (e.g. shelling out to the Keeper Commander CLI, or calling
Keeper's Secrets Manager API) without touching any caller — every caller in
this codebase goes through `get_credentials(vendor_id)`.

Fixture credentials live in fixtures/keeper/test-credentials.json and are
clearly-fake sandbox values only — never a real vendor credential.

Expected import: `from app.config import config` (app/config.py, already
written by another agent as of this pass — importable now).
"""
import json
import os
from typing import Any, Optional

from app.config import config

_fixture_cache: Optional[dict] = None


def _load_fixture() -> dict:
    global _fixture_cache
    if _fixture_cache is not None:
        return _fixture_cache
    file_path = os.path.join(config.fixtures_dir, "keeper", "test-credentials.json")
    with open(file_path, "r", encoding="utf8") as f:
        _fixture_cache = json.load(f)
    return _fixture_cache


async def get_credentials(vendor_id: Any) -> Optional[dict]:
    """Returns {"username": ..., "password": ...} or None."""
    if not config.keeper_stub:
        raise RuntimeError(
            "Real Keeper integration is not wired up. Set KEEPER_STUB=true or implement a real lookup here."
        )
    fixture = _load_fixture()
    return fixture.get(str(vendor_id))
