"""Verify private catalog/index exports and write only a bounded public receipt.

This tool never instantiates a vector client, embeds, publishes or changes a
production route. Use capture_catalog through the trusted operator boundary to
obtain the current MySQL snapshot; a saved snapshot alone is not freshness proof.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_release import CatalogEvidenceError, verify_release


def check_bundle(bundle: dict) -> dict:
    if bundle.get("inventory_complete") is not True:
        raise CatalogEvidenceError("CATALOG_FULL_INVENTORY_REQUIRED")
    if bundle.get("marker_before") != bundle.get("marker_after"):
        raise CatalogEvidenceError("CATALOG_PUBLICATION_CHANGED_DURING_READ")
    return verify_release(bundle["current_snapshot"], bundle["manifest"], bundle["index_records"],
                          published_marker=bundle.get("marker_after"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-bundle", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.private_bundle.resolve() == args.report.resolve():
        parser.error("report must not overwrite the private evidence bundle")
    try:
        bundle = json.loads(args.private_bundle.read_text(encoding="utf-8"))
        report = check_bundle(bundle)
        code = 0
    except CatalogEvidenceError as exc:
        report = {"status": "BLOCKED", "reason_code": str(exc), "live_runtime_verified": False}
        code = 2
    except (OSError, ValueError, TypeError, KeyError):
        report = {"status": "BLOCKED", "reason_code": "CATALOG_EVIDENCE_INPUT_INVALID", "live_runtime_verified": False}
        code = 2
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
