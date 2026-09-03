"""仓库稳定身份地图:汇总 repo_meta_api.jsonl 的 name↔repo_id 观测(identity v2 数据基础)。

上游 GH Archive 历史重建榜只有 full_name,无法区分"仓库改名"与"同名复用"
(audit 报告的 created_at 身份异常)。GitHub API 补全数据(enrich_github_api.py)
每行携带稳定身份(repo_id/node_id/canonical full_name/requested_name),且同一
仓库会被多次抓取(多行历史)。本脚本把全部 name↔id 观测聚合成一张持续积累
的身份地图,作为 identity v2 的主键迁移依据:

  data/raw/repo_id_map.jsonl       每个 name 一行:
      {"name", "repo_id", "node_id", "canonical_name",
       "first_seen", "last_seen", "fetch_count"}
  data/raw/repo_id_anomalies.json  派生异常汇总(附证据,按 key 排序):
      rename_candidates  同一 repo_id 对应多个 name(改名链条)
      reuse_candidates   同一 name 历史上出现多个 repo_id(同名复用)

聚合规则(完全确定、可重复):
  - 分组键 name = requested_name(改名场景请求名 ≠ canonical 名),
    缺 requested_name 的旧行回退 full_name;
  - 行序按 (fetched_at 有效, epoch, 文件行序) 排序:fetched_at 缺失或不可
    解析的行视为最旧,同刻时文件顺序靠后者胜;
  - "最新观测值" = 排序后最后一条"携带该字段"的行(缺字段的行不构成观测,
    例如早期格式无 repo_id 的行不会抹掉更早观测到的 repo_id);
  - first_seen/last_seen 取组内最旧/最新行的原始 fetched_at(最旧行缺失
    则为 null);anomaly 证据基于 (name, repo_id) 全部历史配对,而非仅最新值;
  - 异常检测中"name"取该仓库观测到的全部拼写:分组键(requested_name/full_name)
    与 canonical full_name 都是别名——同一 requested_name 拿到不同 canonical
    (GitHub 重定向)同样说明发生过 rename,只看分组键会漏检;
  - 输出只由输入决定:无时间戳、sort_keys、稳定排序,重跑字节级一致。

fail closed:输入文件缺失、行不可解析、非 JSON 对象、缺少归属 name 或字段
类型不符时报错退出(exit 1),不写任何输出;输出经 atomic_write_text 原子写。

用法:
  python scripts/repo_id_map.py           # 重建两个输出文件并打印摘要
  python scripts/repo_id_map.py --stats   # 只打印摘要,不写文件
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from scripts.atomic_io import atomic_write_text

SOURCE_NAME = "repo_meta_api.jsonl"
MAP_NAME = "repo_id_map.jsonl"
ANOMALIES_NAME = "repo_id_anomalies.json"


class RepoIdMapError(Exception):
    """输入损坏(不可解析/结构不符),fail closed 不产出任何输出。"""


class Observation(NamedTuple):
    """输入文件中的一行身份观测。"""

    name: str              # 分组键:requested_name(缺省回退 full_name)
    full_name: str | None  # API canonical 名
    repo_id: int | None
    node_id: str | None
    fetched_at: str | None  # 原始字符串,原样保留
    ts: float               # fetched_at 的 epoch;缺失/不可解析为 0.0
    ts_present: bool        # fetched_at 是否有效
    idx: int                # 文件行序(0-based),同刻平局时靠后者胜


def _order_key(o: Observation) -> tuple[int, float, int]:
    """缺失 fetched_at 视为最旧(flag=0),同刻时文件行序靠后者胜。"""
    return (1 if o.ts_present else 0, o.ts, o.idx)


def _parse_fetched_at(value: str | None) -> tuple[bool, float]:
    """返回 (有效, epoch)。缺失或不可解析的 ISO 串视为最旧(epoch=0.0)。"""
    if value is None:
        return False, 0.0
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return False, 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return True, dt.timestamp()


def load_observations(path: Path) -> list[Observation]:
    """解析输入 JSONL;任何结构损坏直接抛 RepoIdMapError(fail closed)。"""
    path = Path(path)
    if not path.exists():
        raise RepoIdMapError(f"输入不存在: {path}")
    obs: list[Observation] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RepoIdMapError(f"{path}:{idx + 1} JSON 解析失败: {exc}") from exc
        if not isinstance(row, dict):
            raise RepoIdMapError(f"{path}:{idx + 1} 行不是 JSON 对象")
        requested = row.get("requested_name")
        full_name = row.get("full_name")
        for key, value in (("requested_name", requested), ("full_name", full_name)):
            if value is not None and (not isinstance(value, str) or not value):
                raise RepoIdMapError(f"{path}:{idx + 1} {key} 必须是非空字符串或缺失")
        name = requested or full_name
        if not name:
            raise RepoIdMapError(f"{path}:{idx + 1} 缺少 requested_name/full_name,无法归属")
        repo_id = row.get("repo_id")
        if repo_id is not None and (isinstance(repo_id, bool) or not isinstance(repo_id, int)):
            raise RepoIdMapError(f"{path}:{idx + 1} repo_id 必须是整数或缺失")
        node_id = row.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise RepoIdMapError(f"{path}:{idx + 1} node_id 必须是字符串或缺失")
        fetched_at = row.get("fetched_at")
        if fetched_at is not None and not isinstance(fetched_at, str):
            raise RepoIdMapError(f"{path}:{idx + 1} fetched_at 必须是字符串或缺失")
        ts_present, ts = _parse_fetched_at(fetched_at)
        obs.append(Observation(name, full_name, repo_id, node_id, fetched_at, ts, ts_present, idx))
    return obs


def _span(lines: list[Observation]) -> tuple[str | None, str | None, int]:
    """(first_seen, last_seen, fetch_count):基于已按行序排好的组内观测。"""
    return lines[0].fetched_at, lines[-1].fetched_at, len(lines)


def _latest_field(lines: list[Observation], field: str):
    """排序后最后一条携带该字段的观测值;整组都缺则 None。"""
    for o in reversed(lines):
        value = getattr(o, field)
        if value is not None:
            return value
    return None


def aggregate(obs: list[Observation]) -> tuple[list[dict], dict, dict]:
    """纯函数:观测列表 → (map 行列表, anomalies 对象, 摘要 stats)。

    异常检测按"别名"统计:一条观测为所属 repo_id 贡献两个别名——分组键
    (requested_name,缺省回退 full_name)与 canonical full_name(若存在)。
    """
    ordered = sorted(obs, key=_order_key)
    by_name: dict[str, list[Observation]] = defaultdict(list)
    by_repo: dict[int, list[Observation]] = defaultdict(list)
    by_alias: dict[str, list[Observation]] = defaultdict(list)
    by_repo_alias: dict[int, set[str]] = defaultdict(set)
    by_alias_repo: dict[tuple[str, int], list[Observation]] = defaultdict(list)
    for o in ordered:
        by_name[o.name].append(o)
        if o.repo_id is None:
            continue
        by_repo[o.repo_id].append(o)
        aliases = {o.name} | ({o.full_name} if o.full_name else set())
        for alias in aliases:
            by_alias[alias].append(o)
            by_repo_alias[o.repo_id].add(alias)
            by_alias_repo[(alias, o.repo_id)].append(o)

    entries = []
    for name in sorted(by_name):
        lines = by_name[name]
        first_seen, last_seen, fetch_count = _span(lines)
        entries.append({
            "name": name,
            "repo_id": _latest_field(lines, "repo_id"),
            "node_id": _latest_field(lines, "node_id"),
            "canonical_name": _latest_field(lines, "full_name"),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "fetch_count": fetch_count,
        })

    rename_candidates = []
    for repo_id in sorted(by_repo):
        aliases = sorted(by_repo_alias[repo_id])
        if len(aliases) < 2:
            continue
        rename_candidates.append({
            "repo_id": repo_id,
            "node_id": _latest_field(by_repo[repo_id], "node_id"),
            "names": [{"name": alias, "first_seen": _span(by_alias_repo[(alias, repo_id)])[0],
                       "last_seen": _span(by_alias_repo[(alias, repo_id)])[1],
                       "fetch_count": _span(by_alias_repo[(alias, repo_id)])[2]}
                      for alias in aliases],
        })

    reuse_candidates = []
    for alias in sorted(by_alias):
        ids = sorted({o.repo_id for o in by_alias[alias] if o.repo_id is not None})
        if len(ids) < 2:
            continue
        reuse_candidates.append({
            "name": alias,
            "repo_ids": [{"repo_id": rid, "node_id": _latest_field(by_repo[rid], "node_id"),
                          "first_seen": _span(by_alias_repo[(alias, rid)])[0],
                          "last_seen": _span(by_alias_repo[(alias, rid)])[1],
                          "fetch_count": _span(by_alias_repo[(alias, rid)])[2]}
                         for rid in ids],
        })

    stats = {"observations": len(obs), "unique_names": len(by_name),
             "unique_repo_ids": len(by_repo),
             "rename_candidates": len(rename_candidates),
             "reuse_candidates": len(reuse_candidates)}
    anomalies = {"source": SOURCE_NAME, "stats": stats,
                 "rename_candidates": rename_candidates,
                 "reuse_candidates": reuse_candidates}
    return entries, anomalies, stats


def render_map(entries: list[dict]) -> str:
    return "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries)


def render_anomalies(anomalies: dict) -> str:
    return json.dumps(anomalies, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def rebuild(input_path: Path, map_path: Path, anomalies_path: Path) -> dict:
    """重建两个输出文件并返回摘要;坏输入在任何写出前抛错(fail closed)。"""
    obs = load_observations(input_path)
    entries, anomalies, stats = aggregate(obs)
    map_text = render_map(entries)
    anomalies_text = render_anomalies(anomalies)
    atomic_write_text(Path(map_path), map_text)
    atomic_write_text(Path(anomalies_path), anomalies_text)
    return stats


def collect_stats(input_path: Path) -> dict:
    """只算摘要,不写任何文件(--stats 模式)。"""
    _, _, stats = aggregate(load_observations(input_path))
    return stats


def print_summary(stats: dict) -> None:
    print(f"观测行数: {stats['observations']}")
    print(f"唯一 name: {stats['unique_names']}")
    print(f"唯一 repo_id: {stats['unique_repo_ids']}")
    print(f"rename_candidates(同一 repo_id 多个 name): {stats['rename_candidates']}")
    print(f"reuse_candidates(同一 name 多个 repo_id): {stats['reuse_candidates']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="聚合 repo_meta_api.jsonl 的 name↔repo_id 身份地图")
    ap.add_argument("--stats", action="store_true", help="只打印摘要,不写输出文件")
    args = ap.parse_args(argv)
    src = config.RAW_DIR / SOURCE_NAME
    try:
        if args.stats:
            stats = collect_stats(src)
        else:
            stats = rebuild(src, config.RAW_DIR / MAP_NAME, config.RAW_DIR / ANOMALIES_NAME)
    except RepoIdMapError as exc:
        print(f"repo_id_map: {exc}", file=sys.stderr)
        return 1
    print_summary(stats)
    if not args.stats:
        print(f"已写出: {config.RAW_DIR / MAP_NAME}")
        print(f"        {config.RAW_DIR / ANOMALIES_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
