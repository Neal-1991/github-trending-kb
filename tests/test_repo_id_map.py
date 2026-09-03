"""repo_id_map 聚合与异常检测:合成输入(tmp_path),不读真实 data/,不访问网络。"""
import json

import scripts.repo_id_map as rim
from scripts.repo_id_map import RepoIdMapError


def _row(*, requested=None, full_name=None, repo_id=None, node_id=None, fetched_at=None):
    """构造一行 repo_meta_api 观测(字段缺省即不出现在行里)。"""
    row = {}
    if requested is not None:
        row["requested_name"] = requested
    if full_name is not None:
        row["full_name"] = full_name
    if repo_id is not None:
        row["repo_id"] = repo_id
    if node_id is not None:
        row["node_id"] = node_id
    if fetched_at is not None:
        row["fetched_at"] = fetched_at
    return row


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _read_map(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _read_anomalies(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_basic_aggregation(tmp_path):
    src = _write(tmp_path / "repo_meta_api.jsonl", [
        _row(requested="a/b", full_name="a/b", repo_id=1, node_id="n1",
             fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="a/b", full_name="c/d", repo_id=1, node_id="n1",
             fetched_at="2026-01-02T00:00:00+00:00"),
        _row(requested="a/b", full_name="c/d", repo_id=1, node_id="n1",
             fetched_at="2026-01-03T00:00:00Z"),
    ])
    map_path, anom_path = tmp_path / "repo_id_map.jsonl", tmp_path / "repo_id_anomalies.json"

    stats = rim.rebuild(src, map_path, anom_path)

    assert stats == {"observations": 3, "unique_names": 1, "unique_repo_ids": 1,
                     "rename_candidates": 1, "reuse_candidates": 0}
    entries = _read_map(map_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "a/b"
    assert entry["repo_id"] == 1
    assert entry["node_id"] == "n1"
    assert entry["canonical_name"] == "c/d"  # 最新观测的 canonical 名(改名后)
    assert entry["first_seen"] == "2026-01-01T00:00:00Z"
    assert entry["last_seen"] == "2026-01-03T00:00:00Z"
    assert entry["fetch_count"] == 3
    # requested 名一直是 a/b,但最新观测 canonical 是 c/d(GitHub 重定向)→ 改名证据
    anomalies = _read_anomalies(anom_path)
    assert anomalies["rename_candidates"] == [{
        "repo_id": 1,
        "node_id": "n1",
        "names": [
            {"name": "a/b", "first_seen": "2026-01-01T00:00:00Z",
             "last_seen": "2026-01-03T00:00:00Z", "fetch_count": 3},
            {"name": "c/d", "first_seen": "2026-01-02T00:00:00+00:00",
             "last_seen": "2026-01-03T00:00:00Z", "fetch_count": 2},
        ],
    }]
    assert anomalies["reuse_candidates"] == []


def test_rename_candidates_detected(tmp_path):
    src = _write(tmp_path / "repo_meta_api.jsonl", [
        _row(requested="p/q", full_name="p/q", repo_id=10, node_id="n10",
             fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="r/s", full_name="r/s", repo_id=10, node_id="n10",
             fetched_at="2026-02-01T00:00:00Z"),
    ])
    map_path, anom_path = tmp_path / "repo_id_map.jsonl", tmp_path / "repo_id_anomalies.json"

    stats = rim.rebuild(src, map_path, anom_path)

    assert stats["rename_candidates"] == 1
    assert stats["reuse_candidates"] == 0
    anomalies = _read_anomalies(anom_path)
    # 同一 repo_id 先后挂在两个 name 下(改名链条),按 name 排序
    assert anomalies["rename_candidates"] == [{
        "repo_id": 10,
        "node_id": "n10",
        "names": [
            {"name": "p/q", "first_seen": "2026-01-01T00:00:00Z",
             "last_seen": "2026-01-01T00:00:00Z", "fetch_count": 1},
            {"name": "r/s", "first_seen": "2026-02-01T00:00:00Z",
             "last_seen": "2026-02-01T00:00:00Z", "fetch_count": 1},
        ],
    }]
    assert anomalies["reuse_candidates"] == []


def test_reuse_candidates_detected(tmp_path):
    src = _write(tmp_path / "repo_meta_api.jsonl", [
        _row(requested="old/e", repo_id=100, node_id="n100",
             fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="old/e", repo_id=200, node_id="n200",
             fetched_at="2026-02-01T00:00:00Z"),
    ])
    map_path, anom_path = tmp_path / "repo_id_map.jsonl", tmp_path / "repo_id_anomalies.json"

    stats = rim.rebuild(src, map_path, anom_path)

    assert stats["rename_candidates"] == 0
    assert stats["reuse_candidates"] == 1
    anomalies = _read_anomalies(anom_path)
    assert anomalies["rename_candidates"] == []
    assert anomalies["reuse_candidates"] == [{
        "name": "old/e",
        "repo_ids": [
            {"repo_id": 100, "node_id": "n100", "first_seen": "2026-01-01T00:00:00Z",
             "last_seen": "2026-01-01T00:00:00Z", "fetch_count": 1},
            {"repo_id": 200, "node_id": "n200", "first_seen": "2026-02-01T00:00:00Z",
             "last_seen": "2026-02-01T00:00:00Z", "fetch_count": 1},
        ],
    }]
    # map 行的最新观测值取更新的 id
    entry = _read_map(map_path)[0]
    assert entry["repo_id"] == 200


def test_no_anomalies_when_clean(tmp_path):
    src = _write(tmp_path / "repo_meta_api.jsonl", [
        _row(requested="a/b", repo_id=1, fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="a/b", repo_id=1, fetched_at="2026-01-02T00:00:00Z"),
        _row(requested="c/d", repo_id=2, fetched_at="2026-01-03T00:00:00Z"),
    ])
    map_path, anom_path = tmp_path / "repo_id_map.jsonl", tmp_path / "repo_id_anomalies.json"

    stats = rim.rebuild(src, map_path, anom_path)

    assert stats == {"observations": 3, "unique_names": 2, "unique_repo_ids": 2,
                     "rename_candidates": 0, "reuse_candidates": 0}
    anomalies = _read_anomalies(anom_path)
    assert anomalies["rename_candidates"] == []
    assert anomalies["reuse_candidates"] == []
    assert [e["name"] for e in _read_map(map_path)] == ["a/b", "c/d"]  # 按 name 排序


def test_missing_fetched_at_treated_as_oldest(tmp_path):
    src = _write(tmp_path / "repo_meta_api.jsonl", [
        _row(requested="x/y", repo_id=1, node_id="n1"),  # 无 fetched_at → 最旧
        _row(requested="x/y", repo_id=2, node_id="n2", fetched_at="2026-05-01T00:00:00Z"),
        _row(requested="z/w", repo_id=7),                # 两条缺失行之间:文件顺序靠后者胜
        _row(requested="z/w", repo_id=8),
    ])
    map_path = tmp_path / "repo_id_map.jsonl"

    rim.rebuild(src, map_path, tmp_path / "repo_id_anomalies.json")

    entries = {e["name"]: e for e in _read_map(map_path)}
    assert entries["x/y"]["repo_id"] == 2  # 有时间戳的观测更新
    assert entries["x/y"]["first_seen"] is None  # 最旧行缺失 fetched_at → null
    assert entries["x/y"]["last_seen"] == "2026-05-01T00:00:00Z"
    assert entries["z/w"]["repo_id"] == 8
    assert entries["z/w"]["first_seen"] is None
    assert entries["z/w"]["last_seen"] is None


def test_same_fetched_at_later_file_line_wins(tmp_path):
    src = _write(tmp_path / "repo_meta_api.jsonl", [
        _row(requested="a/b", repo_id=1, node_id="n1", fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="a/b", repo_id=2, node_id="n2", fetched_at="2026-01-01T00:00:00Z"),
    ])
    map_path = tmp_path / "repo_id_map.jsonl"

    rim.rebuild(src, map_path, tmp_path / "repo_id_anomalies.json")

    entry = _read_map(map_path)[0]
    assert entry["repo_id"] == 2
    assert entry["node_id"] == "n2"


def test_out_of_order_input_deterministic(tmp_path):
    """时间戳互不相同时,打乱输入行序不改变任何输出字节。"""
    rows = [
        _row(requested="a/b", repo_id=1, node_id="n1", fetched_at="2026-01-02T00:00:00Z"),
        _row(requested="a/b", repo_id=1, node_id="n1", fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="c/d", repo_id=2, node_id="n2", fetched_at="2026-01-03T00:00:00Z"),
        _row(requested="e/f", repo_id=3, node_id="n3", fetched_at="2026-01-04T00:00:00Z"),
        _row(requested="e/g", repo_id=3, node_id="n3", fetched_at="2026-01-05T00:00:00Z"),
    ]
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    _write(dir_a / "repo_meta_api.jsonl", rows)
    _write(dir_b / "repo_meta_api.jsonl", list(reversed(rows)))

    stats_a = rim.rebuild(dir_a / "repo_meta_api.jsonl",
                          dir_a / "repo_id_map.jsonl", dir_a / "repo_id_anomalies.json")
    stats_b = rim.rebuild(dir_b / "repo_meta_api.jsonl",
                          dir_b / "repo_id_map.jsonl", dir_b / "repo_id_anomalies.json")

    assert stats_a == stats_b
    assert (dir_a / "repo_id_map.jsonl").read_bytes() == \
           (dir_b / "repo_id_map.jsonl").read_bytes()
    assert (dir_a / "repo_id_anomalies.json").read_bytes() == \
           (dir_b / "repo_id_anomalies.json").read_bytes()


def test_idempotent_rerun_bytes_identical(tmp_path):
    src = _write(tmp_path / "repo_meta_api.jsonl", [
        _row(requested="a/b", repo_id=1, node_id="n1", fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="c/d", full_name="e/f", repo_id=2, node_id="n2",
             fetched_at="2026-01-02T00:00:00Z"),
        _row(requested="c/d", full_name="c/d", repo_id=2, node_id="n2",
             fetched_at="2026-01-03T00:00:00Z"),
    ])
    map_path, anom_path = tmp_path / "repo_id_map.jsonl", tmp_path / "repo_id_anomalies.json"

    rim.rebuild(src, map_path, anom_path)
    first_map, first_anom = map_path.read_bytes(), anom_path.read_bytes()
    rim.rebuild(src, map_path, anom_path)

    assert map_path.read_bytes() == first_map
    assert anom_path.read_bytes() == first_anom


def test_fail_closed_on_bad_json_line(tmp_path):
    src = tmp_path / "repo_meta_api.jsonl"
    src.write_text(json.dumps(_row(requested="a/b", repo_id=1)) + "\n" + "{oops\n",
                   encoding="utf-8")
    map_path, anom_path = tmp_path / "repo_id_map.jsonl", tmp_path / "repo_id_anomalies.json"

    try:
        rim.rebuild(src, map_path, anom_path)
    except RepoIdMapError as exc:
        assert "JSON 解析失败" in str(exc)
    else:
        raise AssertionError("坏行未触发 fail closed")
    assert not map_path.exists() and not anom_path.exists()  # 不产出半成品


def test_fail_closed_on_structural_breaks(tmp_path):
    cases = [
        [42],                                     # 非 JSON 对象
        [_row(repo_id=1)],                        # 无 requested_name/full_name
        [_row(requested="a/b", full_name="a/b", repo_id="1")],  # repo_id 类型不符
        [_row(requested="a/b", fetched_at=123)],  # fetched_at 类型不符
    ]
    for i, rows in enumerate(cases):
        src = _write(tmp_path / f"case{i}" / "repo_meta_api.jsonl", rows)
        map_path = tmp_path / f"case{i}" / "repo_id_map.jsonl"
        anom_path = tmp_path / f"case{i}" / "repo_id_anomalies.json"
        try:
            rim.rebuild(src, map_path, anom_path)
        except RepoIdMapError:
            pass
        else:
            raise AssertionError(f"case{i} 未触发 fail closed")
        assert not map_path.exists() and not anom_path.exists()


def test_missing_input_fail_closed(tmp_path):
    src = tmp_path / "repo_meta_api.jsonl"  # 未创建
    map_path = tmp_path / "repo_id_map.jsonl"

    try:
        rim.rebuild(src, map_path, tmp_path / "repo_id_anomalies.json")
    except RepoIdMapError as exc:
        assert "输入不存在" in str(exc)
    else:
        raise AssertionError("缺失输入未触发 fail closed")
    assert not map_path.exists()


def test_stats_mode_does_not_write_files(sandbox, capsys):
    _write(sandbox["raw"] / "repo_meta_api.jsonl", [
        _row(requested="a/b", repo_id=1, node_id="n1", fetched_at="2026-01-01T00:00:00Z"),
        _row(requested="c/d", repo_id=2, node_id="n2", fetched_at="2026-01-02T00:00:00Z"),
    ])

    assert rim.main(["--stats"]) == 0

    assert not (sandbox["raw"] / "repo_id_map.jsonl").exists()
    assert not (sandbox["raw"] / "repo_id_anomalies.json").exists()
    out = capsys.readouterr().out
    assert "观测行数: 2" in out
    assert "唯一 name: 2" in out
    assert "唯一 repo_id: 2" in out


def test_main_rebuild_writes_sandbox_outputs(sandbox):
    _write(sandbox["raw"] / "repo_meta_api.jsonl", [
        _row(requested="a/b", repo_id=1, node_id="n1", fetched_at="2026-01-01T00:00:00Z"),
    ])

    assert rim.main([]) == 0

    entries = _read_map(sandbox["raw"] / "repo_id_map.jsonl")
    assert len(entries) == 1 and entries[0]["name"] == "a/b"
    assert (sandbox["raw"] / "repo_id_anomalies.json").exists()


def test_main_missing_input_exits_nonzero(sandbox, capsys):
    assert rim.main([]) == 1  # sandbox raw 目录里没有 repo_meta_api.jsonl
    assert "输入不存在" in capsys.readouterr().err
