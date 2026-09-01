"""只读数据审计:JSONL/CSV 可解析性、重复与冲突、DB 完整性、身份异常、口径统计。

用法:
  python scripts/audit_data.py            # 全量审计,打印报告
  python scripts/audit_data.py --json     # 机器可读输出(CI 用)

审计不修改任何数据。发现硬错误(JSONL 不可解析等)退出码 1;
质量问题(重复元数据、身份异常、口径污染)以 findings 列出,退出码 0,
供人工决策,不自动修复。
"""
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DAILY_DIR, DB_PATH, PROFILE_DIR, RAW_DIR, README_DIR
from scripts.snapshot_store import SnapshotValidationError, iter_snapshots

CONFLICT_FIELDS = ["description", "language", "stargazers_count", "created_at",
                   "license_key", "fork", "archived", "homepage"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_jsonl(path: Path, errors: list, findings: list) -> int:
    if not path.exists():
        return 0
    n = 0
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
            n += 1
        except json.JSONDecodeError as e:
            errors.append(f"{path.name}:{i} JSON 解析失败: {e}")
    return n


def audit_snapshot_csv(errors: list, findings: list) -> dict:
    path = RAW_DIR / "repo_meta_snapshot.csv"
    if not path.exists():
        return {}
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    by_name = defaultdict(list)
    for r in rows:
        by_name[r["full_name"]].append(r)
    dups = {k: v for k, v in by_name.items() if len(v) > 1}
    conflicts = []
    for k, rs in dups.items():
        variants = {tuple(r.get(f) or "" for f in CONFLICT_FIELDS) for r in rs}
        if len(variants) > 1:
            conflicts.append(k)
    if dups:
        findings.append(f"repo_meta_snapshot.csv: {len(dups)} 个仓库重复"
                        f"(多 {len(rows) - len(by_name)} 行),其中 {len(conflicts)} 个字段冲突;"
                        f"导入为 first-wins,应按 fetched_at 确定性合并")
    return {"rows": len(rows), "unique": len(by_name), "dups": len(dups), "conflicts": len(conflicts)}


def audit_canonical_snapshots(errors: list, findings: list) -> dict:
    """canonical 是每日榜主来源，结构/内容哈希异常属于硬错误；抓取日缺快照属于质量发现。"""
    snapshots = []
    try:
        snapshots = list(iter_snapshots())
    except (SnapshotValidationError, OSError) as exc:
        errors.append(f"canonical 快照校验失败: {exc}")
    snapshot_dates = {snapshot["date"] for snapshot in snapshots}
    legacy_only = set()
    if DAILY_DIR.joinpath("trends.jsonl").exists():
        for line in DAILY_DIR.joinpath("trends.jsonl").read_text(
                encoding="utf-8").splitlines():
            if not line.strip():
                continue
            date = json.loads(line).get("date")
            if date and date not in snapshot_dates:
                legacy_only.add(date)
    if legacy_only:
        findings.append(f"{len(legacy_only)} 个抓取日缺 canonical 快照(仅 trends.jsonl 兜底): "
                        f"{sorted(legacy_only)};正常情况下每次抓取都会先落快照")
    return {
        "files": len(snapshots),
        "dates": len(snapshot_dates),
        "lists": sum(len(snapshot["lists"]) for snapshot in snapshots),
        "legacy_only_dates": len(legacy_only),
    }


def audit_db(errors: list, findings: list) -> dict:
    if not DB_PATH.exists():
        errors.append(f"数据库不存在: {DB_PATH}")
        return {}
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = {}

    out["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if out["integrity"] != "ok":
        errors.append(f"数据库 integrity_check 失败: {out['integrity']}")
        return out

    for table in ("repos", "trend_daily", "profiles", "push_log"):
        out[f"{table}_rows"] = conn.execute(f"SELECT count(*) c FROM {table}").fetchone()["c"]
    orphan = conn.execute("""
      SELECT count(*) c FROM trend_daily t
      WHERE NOT EXISTS (SELECT 1 FROM repos r WHERE r.full_name = t.full_name)""").fetchone()["c"]
    out["trend_orphans"] = orphan
    if orphan:
        errors.append(f"trend_daily 中 {orphan} 条记录在 repos 无对应行(应满足 repos ⊇ trend_daily)")

    # 身份异常:当前仓库创建时间晚于历史首次上榜 → 同名复用/迁移串档
    rows = conn.execute("""
      SELECT full_name, substr(created_at,1,10) created_at, first_trend_date
      FROM repos
      WHERE created_at IS NOT NULL AND first_trend_date IS NOT NULL
        AND substr(created_at,1,10) > first_trend_date""").fetchall()
    out["identity_anomalies"] = [dict(r) for r in rows]
    if rows:
        findings.append(f"{len(rows)} 个仓库 created_at 晚于 first_trend_date"
                        f"(疑似同名复用/仓库迁移串档,如 Jarred-Sumner/bun);"
                        f"在引入 repository_id 身份前,相关画像与历史统计不可信")

    # 口径污染:partial 月仅 Top10 可信,但 rank>10 记录参与了聚合
    out["partial_rank_gt10"] = conn.execute(
        "SELECT count(*) c FROM trend_daily WHERE quality='partial' AND rank>10").fetchone()["c"]
    if out["partial_rank_gt10"]:
        findings.append(f"raw 中保留 {out['partial_rank_gt10']} 条 partial 且 rank>10 的记录;"
                        f"trusted 聚合会排除这些记录(partial 口径仅 Top10 可信)")

    # profile 状态:README 缺失清单未落到 profile_status
    no_readme = conn.execute(
        "SELECT count(*) c FROM repos WHERE profile_status='no_readme'").fetchone()["c"]
    missing_file = README_DIR / "_missing.txt"
    missing_n = 0
    if missing_file.exists():
        missing_n = sum(
            1 for line in missing_file.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    out["no_readme_status"], out["missing_txt"] = no_readme, missing_n
    if missing_n and not no_readme:
        findings.append(f"_missing.txt 有 {missing_n} 条,但库内 profile_status='no_readme' 为 0"
                        f"(README 状态机未落库)")

    # 画像积压:出现在真实抓取日(quality 为空)但仍是 pending 的仓库,
    # 反映单轮 MAX_NEW_PROFILES 截断后的 backlog 深度
    out["profile_backlog"] = conn.execute("""
      SELECT count(DISTINCT t.full_name) c FROM trend_daily t
      JOIN repos r ON r.full_name = t.full_name
      WHERE t.quality IS NULL AND r.profile_status = 'pending'""").fetchone()["c"]
    if out["profile_backlog"]:
        findings.append(f"{out['profile_backlog']} 个真实抓取仓库画像仍为 pending"
                        f"(单轮 80 上限的待补 backlog,超过 500 建议调大上限)")

    out["core_days_mixed"] = conn.execute("""
      SELECT count(*) c FROM repos r WHERE r.core_days != (
        SELECT COUNT(DISTINCT date) FROM trend_daily t
        WHERE t.full_name = r.full_name AND t.rank <= 10 AND t.list_type='arch:total'
          AND (t.quality IS NULL OR t.quality='full'
               OR (t.quality='partial' AND t.rank <= 10)))""").fetchone()["c"]
    if out["core_days_mixed"]:
        findings.append(f"{out['core_days_mixed']} 个仓库 core_days 混入了真实榜/语言榜"
                        f"(页面口径为历史重建榜 Top10)")
    conn.close()
    return out


def audit_daily_mismatch(findings: list) -> dict:
    """历史已知:2026-08-30 落盘与推送不是同一快照(只报告,不自动改写)。"""
    trends, push_log = DAILY_DIR / "trends.jsonl", DAILY_DIR / "push_log.jsonl"
    if not (trends.exists() and push_log.exists()):
        return {}
    by_date_disk, by_date_push = defaultdict(set), defaultdict(set)
    for line in trends.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        for e in rec["entries"]:
            by_date_disk[rec["date"]].add((rec["list_type"], e["repo"]))
    for line in push_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        by_date_push[r["date"]].add((r["list_type"], r["full_name"]))
    out = {}
    for d in sorted(set(by_date_disk) | set(by_date_push)):
        disk, push = by_date_disk.get(d, set()), by_date_push.get(d, set())
        if disk != push:
            info = {"date": d, "disk": len(disk), "pushed": len(push),
                    "disk_only": len(disk - push), "push_only": len(push - disk)}
            out[d] = info
            findings.append(f"{d} 落盘 {info['disk']} 条 vs 推送 {info['pushed']} 条"
                            f"(仅落盘 {info['disk_only']},仅推送 {info['push_only']})"
                            f"→ 当日推送不可由 source 完整复现,保留为审计事实")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    errors, findings = [], []
    counts = {}
    for p in [RAW_DIR / "repo_meta_api.jsonl", RAW_DIR / "repo_gone.jsonl",
              DAILY_DIR / "trends.jsonl", DAILY_DIR / "push_log.jsonl",
              PROFILE_DIR / "profiles.jsonl"]:
        counts[p.name] = audit_jsonl(p, errors, findings)
    counts["repo_meta_snapshot.csv"] = audit_snapshot_csv(errors, findings)
    counts["canonical_snapshots"] = audit_canonical_snapshots(errors, findings)
    db_info = audit_db(errors, findings)
    mismatch = audit_daily_mismatch(findings)

    report = {"errors": errors, "findings": findings, "jsonl_counts": counts,
              "db": {k: v for k, v in db_info.items() if not k.startswith("identity")},
              "identity_anomalies": db_info.get("identity_anomalies", []),
              "daily_mismatch": mismatch,
              "file_hashes": {str(p): sha256_file(p) for p in
                              [RAW_DIR / "trends_gharchive.csv", RAW_DIR / "repo_meta_snapshot.csv",
                               DAILY_DIR / "trends.jsonl", PROFILE_DIR / "profiles.jsonl"]
                              if p.exists()}}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("== 硬错误 ==")
        print("\n".join(errors) or "(无)")
        print("\n== 质量发现 ==")
        print("\n".join(f"- {f}" for f in findings) or "(无)")
        print("\n== 计数 ==")
        for k, v in counts.items():
            print(f"  {k}: {v}")
        print(f"  db integrity: {db_info.get('integrity')}")
        sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
