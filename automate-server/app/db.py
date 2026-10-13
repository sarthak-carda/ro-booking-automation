"""Merges src/db.js + src/lib/store.js: named JSON-file-backed collections.

Tiny JSON-file-backed collection store — callers only see
get/list/upsert/set/delete, same contract as the Node `makeCollection`.
"""
import json
import os
from pathlib import Path
from typing import Any, Optional

from app.config import config

_cache: dict[str, dict[str, Any]] = {}


def _file_path_for(name: str) -> str:
    return os.path.join(config.data_dir, f"{name}.json")


def _load(name: str) -> dict[str, Any]:
    if name in _cache:
        return _cache[name]
    file_path = _file_path_for(name)
    data: dict[str, Any] = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    _cache[name] = data
    return data


def _persist(name: str) -> None:
    if not os.path.exists(config.data_dir):
        Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    with open(_file_path_for(name), "w", encoding="utf8") as f:
        json.dump(_cache.get(name, {}), f, indent=2)


class Collection:
    def __init__(self, name: str) -> None:
        self.name = name

    def get(self, id: Any) -> Optional[dict]:
        return _load(self.name).get(str(id))

    def list(self) -> list:
        return list(_load(self.name).values())

    def upsert(self, id: Any, record: dict) -> dict:
        data = _load(self.name)
        key = str(id)
        existing = data.get(key) or {}
        merged = {**existing, **record, "id": id}
        data[key] = merged
        _persist(self.name)
        return merged

    def set(self, id: Any, record: dict) -> dict:
        data = _load(self.name)
        key = str(id)
        merged = {**record, "id": id}
        data[key] = merged
        _persist(self.name)
        return merged

    def delete(self, id: Any) -> None:
        data = _load(self.name)
        data.pop(str(id), None)
        _persist(self.name)

    def _reset(self) -> None:
        """Test-only: drop the in-memory cache so a fresh read hits disk (or nothing)."""
        _cache.pop(self.name, None)


# Per-vendor health + config: { id (vendorId), name, portalUrl, requiresCredentials, status, runs: [{outcome, at}] }
vendors = Collection("vendors")

# Pipeline + booking runs: { id, kind: "pipeline"|"booking", vendorId, status, steps: [...], createdAt, updatedAt }
runs = Collection("runs")

# Shipped scripts keyed by normalized vendor host: { id (host), vendorId, source, version, shippedAt, auditReport }
script_registry = Collection("script_registry")

# Self-heal items: { id, vendorId, scriptHost, attempts, cap, status: "healing"|"resolved"|"escalated", diagnoses: [...], referenceScreenshot, failureScreenshot }
self_heal_queue = Collection("self_heal_queue")
