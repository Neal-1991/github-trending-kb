"""仓库身份/元数据冲突的证据化标注:生成 data/raw/identity_flags.json。

输入全部只读:repo_meta_snapshot.csv、repo_meta_api.jsonl 两个 raw 元数据源,
加 scripts.db.connect_ro 的只读 DB(判 created_after_trend)。产出单个 JSON
标注文件(原子写出,scripts/atomic_io),由 db.rebuild 摄取到
repos.identity_note;文件缺失=无标注,JSON 不可解析时 rebuild fail closed。

flags 判定规则:
- meta_conflict:同一仓库在单一元数据源(snapshot CSV 或 api JSONL)内多行,
  且 CONFLICT_FIELDS 中有字段取值不一致。跨源字段漂移不触发——API 最新观测
  按 fetched_at 覆盖快照是 db.py 的既定合并行为(audit_data.audit_snapshot_csv
  的口径一致,只报来源内部重复/冲突);api 多行为历史观测,同源多观测间字段
  变化仍标注供人工核对。evidence 记录冲突字段 → 各观测取值(标明来自
  snapshot 还是 api、api 的 fetched_at),库内实际取值为最新 API 观测或快照首行。
- created_after_trend:DB 中 created_at 晚于 first_trend_date(疑似改名/同名
  仓库复用),与 Web 详情页 identity_risk 提示同口径:历史记录可能对应同名
  的其他仓库。

用法:
  python scripts/identity_flags.py            # 默认路径,写 data/raw/identity_flags.json
  python scripts/identity_flags.py --json     # 结果打印到 stdout,不落盘
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.atomic_io import atomic_write_json
from scripts.db import connect_ro

# 与 audit_data.CONFLICT_FIELDS 一致(注意是 homepage 而非 homeport)
CONFLICT_FIELDS = ["description", "language", "stargazers_count", "created_at",
                   "license_key", "fork", "archived", "homepage"]

# api JSONL 字段名 → CONFLICT_FIELDS 口径
_API_FIELD_ALIASES = {"stars": "stargazers_count", "license": "license_key"}


def load_snapshot_rows(path: Path) -> list[dict]:
    """读 repo_meta_snapshot.csv(含重复行);文件缺失视为无该来源。"""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_api_records(path: Path) -> list[dict]:
    """读 repo_meta_api.jsonl(全部历史观测,不做合并);文件缺失视为无该来源。"""
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _field_value(obs: dict, field: str):
    """按 CONFLICT_FIELDS 口径取一条观测的原始值;api 缺字段返回 None。"""
    row = obs["row"]
    if obs["source"] == "api":
        return row.get(_API_FIELD_ALIASES.get(field, field))
    return row.get(field)


def _norm(field: str, value) -> str:
    """比较用归一化:布尔统一 true/false,数值去无效零;其余 strip 后原样比较。

    只用于同源多行间的相等判断(snapshot/api 各自格式一致);evidence 保留原始值。
    """
    if value is None:
        return ""
    if field in ("fork", "archived"):
        return "true" if str(value).strip().lower() in ("true", "1") else "false"
    s = str(value).strip()
    if field == "stargazers_count":
        try:
            return str(int(float(s)))
        except ValueError:
            return s
    return s


def _obs_sort_key(obs: dict):
    """evidence 观测顺序:snapshot 在前(无时间),api 按 fetched_at 升序。"""
    return (obs["source"] != "snapshot", obs["fetched_at"] or "")


def detect_meta_conflicts(snapshot_rows: list[dict], api_records: list[dict]) -> dict[str, dict]:
    """meta_conflict 判定(纯函数,输入为已加载的记录列表)。

    返回 {full_name: {"flags": ["meta_conflict"], "note": ..., "evidence": {...}}};
    仅对有冲突的仓库产出条目。冲突按来源内部判定:snapshot 组内、api 组内各自
    比较 CONFLICT_FIELDS;api 组观测按 fetched_at 排序后仍全量列入 evidence。
    """
    obs_by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in snapshot_rows:
        obs_by_repo[r["full_name"]].append({"source": "snapshot", "fetched_at": None, "row": r})
    for m in api_records:
        obs_by_repo[m["full_name"]].append(
            {"source": "api", "fetched_at": m.get("fetched_at"), "row": m})

    out: dict[str, dict] = {}
    for name in sorted(obs_by_repo):
        obs = obs_by_repo[name]
        conflicted: list[str] = []
        src_bits: list[str] = []
        for source, bit in (("snapshot", "快照 CSV"), ("api", "API 历史观测")):
            group = [o for o in obs if o["source"] == source]
            if len(group) < 2:
                continue
            fields = [f for f in CONFLICT_FIELDS
                      if len({_norm(f, _field_value(o, f)) for o in group}) > 1]
            if fields:
                conflicted.extend(f for f in fields if f not in conflicted)
                src_bits.append(bit)
        if not conflicted:
            continue
        evidence = {}
        for f in conflicted:
            entries = []
            for o in sorted(obs, key=_obs_sort_key):
                v = _field_value(o, f)
                if o["source"] == "api" and v is None:
                    continue  # 早期 api 观测可能缺该字段
                e = {"source": o["source"], "value": v}
                if o["fetched_at"]:
                    e["fetched_at"] = o["fetched_at"]
                entries.append(e)
            evidence[f] = entries
        out[name] = {
            "flags": ["meta_conflict"],
            "note": (f"元数据多行冲突({'、'.join(src_bits)}):{', '.join(conflicted)} 取值不一致;"
                     f"库内取值为最新 API 观测或快照首行,各观测值见 evidence"),
            "evidence": {"meta_conflict": evidence},
        }
    return out


def detect_created_after_trend(repo_rows) -> dict[str, dict]:
    """created_after_trend 判定(纯函数,输入为含 full_name/created_at/first_trend_date 的行)。

    与 web/app.py 的 identity_risk、audit_data.audit_db 同口径:
    substr(created_at,1,10) > first_trend_date。任一日期缺失不判定。
    """
    out: dict[str, dict] = {}
    for r in repo_rows:
        created, first = r["created_at"], r["first_trend_date"]
        if not created or not first:
            continue
        if created[:10] <= first:
            continue
        out[r["full_name"]] = {
            "flags": ["created_after_trend"],
            "note": (f"仓库创建日期 {created[:10]} 晚于首次上榜日期 {first},"
                     f"历史记录可能对应同名的其他仓库,请谨慎解读历史指标"),
            "evidence": {"created_after_trend": {"created_at": created, "first_trend_date": first}},
        }
    return out


def build_identity_flags(snapshot_rows: list[dict], api_records: list[dict], repo_rows,
                         *, generated_at: str | None = None) -> dict:
    """汇总两类 flag(纯函数):同一仓库多 flag 时合并 flags/note/evidence。"""
    repos: dict[str, dict] = {}
    for detected in (detect_meta_conflicts(snapshot_rows, api_records),
                     detect_created_after_trend(repo_rows)):
        for name, entry in detected.items():
            cur = repos.setdefault(name, {"flags": [], "note": "", "evidence": {}})
            cur["flags"].extend(entry["flags"])
            cur["note"] = ";".join(p for p in (cur["note"], entry["note"]) if p)
            cur["evidence"].update(entry["evidence"])
    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "repos": repos,
    }


def generate(snapshot_path: Path, api_path: Path, db_path: Path) -> dict:
    """加载三个只读输入并生成标注(不落盘);DB 必须已重建(connect_ro 缺库即抛错)。"""
    conn = connect_ro(db_path)
    try:
        repo_rows = conn.execute(
            "SELECT full_name, created_at, first_trend_date FROM repos").fetchall()
    finally:
        conn.close()
    return build_identity_flags(load_snapshot_rows(snapshot_path), load_api_records(api_path),
                                repo_rows)


def _default_paths() -> tuple[Path, Path, Path, Path]:
    """调用时读取 config 当前值(与 scripts.db._default_sources 同风格)。"""
    import config
    raw = Path(config.RAW_DIR)
    return (raw / "repo_meta_snapshot.csv", raw / "repo_meta_api.jsonl",
            Path(config.DB_PATH), raw / "identity_flags.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成仓库身份/元数据冲突标注 identity_flags.json")
    ap.add_argument("--out", default=None, help="输出路径(默认 data/raw/identity_flags.json)")
    ap.add_argument("--json", action="store_true", help="打印 JSON 到 stdout,不写文件")
    args = ap.parse_args(argv)

    snap, api, db, default_out = _default_paths()
    flags = generate(snap, api, db)
    if args.json:
        print(json.dumps(flags, ensure_ascii=False, indent=1))
        return 0
    out = Path(args.out) if args.out else default_out
    atomic_write_json(out, flags, ensure_ascii=False, indent=1)
    counts: dict[str, int] = defaultdict(int)
    multi = 0
    for entry in flags["repos"].values():
        for f in entry["flags"]:
            counts[f] += 1
        if len(entry["flags"]) > 1:
            multi += 1
    print(f"identity_flags: 标注 {len(flags['repos'])} 个仓库 → {out}")
    for flag, n in sorted(counts.items()):
        print(f"  {flag}: {n}")
    if multi:
        print(f"  (其中 {multi} 个仓库同时命中多类 flag)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
