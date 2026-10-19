#!/usr/bin/env python3
"""Run the automated pipeline for ONE vendor from a reference (a Prometheus bundle or a
DevTools Recorder export) and report whether it produced a script that passes the audit.

    ./.venv/bin/python3 scripts/run_reference_pipeline.py \\
        --vendor bakeoff/vendors/prado-70567.bakeoff.json \\
        --reference ~/Downloads/prometheus-script-entradas.zip \\
        --booking bakeoff/bookings/prado-70567-1.json \\
        --booking bakeoff/bookings/prado-70567-2.json

Needs a live LLM key (ANTHROPIC_API_KEY) — the offline mock cannot read a reference. Set
AUDIT_HEADLESS=false for vendors that block headless browsers. Runs against an ISOLATED
data dir (a fresh temp dir unless --data-dir is given) so it never touches the real script
registry, and never ships unless --auto-ship is passed: the default stops at "awaiting
review", because a human check in a real browser is still required before shipping.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _parse():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vendor", required=True, help="bakeoff vendor config (.bakeoff.json)")
    ap.add_argument("--reference", required=True, help="Prometheus bundle (dir/zip) or DevTools Recorder .json")
    ap.add_argument("--booking", required=True, action="append",
                    help="sample booking JSON; pass twice (2nd is the overfit check)")
    ap.add_argument("--out", default=None, help="directory for the generated script + report")
    ap.add_argument("--data-dir", default=None, help="isolated data dir (default: fresh temp dir)")
    ap.add_argument("--auto-ship", action="store_true", help="ship into the registry if the audit passes")
    return ap.parse_args()


async def _run(args) -> int:
    from app.db import runs
    from app.llm.client import has_live_llm
    from app.pipeline.capture import reference_from_path
    from app.pipeline.run_pipeline import run_vendor_pipeline

    if not has_live_llm():
        print("No live LLM key configured — reference-based generation needs one.", file=sys.stderr)
        return 2

    cfg = json.loads(Path(args.vendor).read_text(encoding="utf8"))
    vendor = {
        "id": cfg["vendorId"],
        "name": cfg["vendorName"],
        "portalUrl": cfg["portalUrl"],
        "requiresCredentials": bool(cfg.get("requiresCredentials")),
    }
    bookings = [json.loads(Path(b).read_text(encoding="utf8")) for b in args.booking[:2]]
    reference = reference_from_path(args.reference, field_mapping=cfg.get("fieldValueKeys"))

    result = await run_vendor_pipeline(
        vendor=vendor,
        sample_booking=bookings[0],
        second_sample_booking=bookings[1] if len(bookings) > 1 else None,
        reference=reference,
        auto_ship=args.auto_ship,
        reviewed_by="reference-pipeline-script",
    )

    run = runs.get(result["runId"]) or {}
    steps = [{"name": s["name"], "status": s["status"]} for s in run.get("steps", [])]
    summary = {
        "vendor": vendor["name"],
        "status": result["status"],
        "reason": result.get("reason"),
        "steps": steps,
        "script_chars": len(result.get("script") or ""),
        "audit": {k: (result.get("audit") or {}).get(k) for k in ("pass", "status", "error")},
        "secondAudit": {k: (result.get("secondAudit") or {}).get(k) for k in ("pass", "status", "error")}
        if result.get("secondAudit") else None,
    }
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="reference-run-out-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{vendor['id']}-script.js").write_text(result.get("script") or "", encoding="utf8")
    (out_dir / f"{vendor['id']}-report.json").write_text(json.dumps({**summary, "run": run}, indent=2, default=str), encoding="utf8")
    print(json.dumps(summary, indent=2))
    print(f"\nscript + report written to {out_dir}")
    return 0 if result["status"] in ("awaiting_review", "shipped") else 1


def main() -> int:
    args = _parse()
    data_dir = args.data_dir or tempfile.mkdtemp(prefix="reference-run-data-")
    os.environ["AUTOMATE_DATA_DIR"] = data_dir.rstrip("/") + "/"  # must be set before app.config imports
    sys.path.insert(0, str(ROOT))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
