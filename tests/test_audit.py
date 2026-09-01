"""audit_data 扩展检查:canonical 覆盖率与抓取日兜底口径。"""
import json

import scripts.audit_data as audit_data


def _write_compat_trends(sandbox, date: str):
    (sandbox["daily"] / "trends.jsonl").write_text(
        json.dumps({"date": date, "list_type": "total", "entries": []}) + "\n",
        encoding="utf-8")


def test_canonical_coverage_reports_uncovered_dates(sandbox, monkeypatch):
    monkeypatch.setattr(audit_data, "DAILY_DIR", sandbox["daily"])
    _write_compat_trends(sandbox, "2026-09-01")
    errors, findings = [], []
    out = audit_data.audit_canonical_snapshots(errors, findings)
    assert out["files"] == 0
    assert out["legacy_only_dates"] == 1
    assert findings and "2026-09-01" in findings[0]


def test_canonical_coverage_clean_when_snapshot_covers(sandbox, monkeypatch):
    from scripts.snapshot_store import build_snapshot, save_snapshot
    monkeypatch.setattr(audit_data, "DAILY_DIR", sandbox["daily"])
    save_snapshot(build_snapshot("2026-09-01", [{"list_type": "total", "entries": []}]))
    _write_compat_trends(sandbox, "2026-09-01")
    errors, findings = [], []
    out = audit_data.audit_canonical_snapshots(errors, findings)
    assert out["dates"] == 1
    assert out["legacy_only_dates"] == 0
    assert not findings
