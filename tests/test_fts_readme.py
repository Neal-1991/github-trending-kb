"""search_fts 的 readme 列:README 正文纳入全文索引。

覆盖:README 独有关键词命中(trigram 英文/中文)、无 README 仓库不报错、
超长 README 按 FTS_README_MAX_CHARS 截断、无法解码的 README 回退空串。
全部离线:sandbox 把 README_DIR 切到 tmp_path,数据来自 write_source_files。
"""
import pytest

from scripts import db
from tests.conftest import write_source_files

# README 内独有关键词,不与 write_source_files 生成的 description/topics/profiles 重叠
KEYWORD_EN = "zephyrioncore"
KEYWORD_ZH = "量子锻造"

# unicode61 回退(老 SQLite)不支持中文子串,中文断言仅在 trigram 可用时执行
HAS_TRIGRAM = db.sqlite_version() >= (3, 34, 0)


def _match_names(conn, query: str) -> set[str]:
    return {r["full_name"] for r in conn.execute(
        "SELECT full_name FROM search_fts WHERE search_fts MATCH ?", (query,))}


def _readme_of(conn, full_name: str) -> str:
    return conn.execute("SELECT readme FROM search_fts WHERE full_name=?",
                        (full_name,)).fetchone()[0]


def test_readme_keyword_hit(sandbox):
    write_source_files(sandbox)
    (sandbox["readmes"] / "owner0__repo0.md").write_text(
        f"# Demo\n{KEYWORD_EN} 是一个{KEYWORD_ZH}框架。\n", encoding="utf-8")
    conn = db.rebuild()
    try:
        assert _match_names(conn, KEYWORD_EN) == {"owner0/repo0"}
        assert _match_names(conn, KEYWORD_ZH) == {"owner0/repo0"}
    finally:
        conn.close()


@pytest.mark.skipif(not HAS_TRIGRAM, reason="unicode61 回退不支持中文子串")
def test_missing_readme_is_empty_not_error(sandbox):
    names = write_source_files(sandbox, repos=3)
    (sandbox["readmes"] / "owner0__repo0.md").write_text("readme only", encoding="utf-8")
    conn = db.rebuild()  # owner1/owner2 无 README,rebuild 不得失败
    try:
        assert _readme_of(conn, "owner1/repo1") == ""
        assert _readme_of(conn, "owner2/repo2") == ""
        assert _readme_of(conn, "owner0/repo0") == "readme only"
        # 无 README 的仓库仍可按既有列命中,FTS 行数不缺
        assert _match_names(conn, "desc") == set(names)
        assert conn.execute("SELECT count(*) FROM search_fts").fetchone()[0] == len(names)
    finally:
        conn.close()


def test_overlong_readme_truncated(sandbox):
    write_source_files(sandbox)
    inside, outside = "insidetoken", "outsidetoken"
    # head 长度 < 6000,outside 关键词起始位置 > 6000(被截断丢弃)
    head = f"{inside} 在前段。" + "x" * (db.FTS_README_MAX_CHARS - 30)
    tail = "y" * 50 + f" {outside} 尾段"
    assert len(head) < db.FTS_README_MAX_CHARS
    assert len(head) + 51 > db.FTS_README_MAX_CHARS
    (sandbox["readmes"] / "owner0__repo0.md").write_text(head + tail, encoding="utf-8")
    conn = db.rebuild()
    try:
        assert _match_names(conn, inside) == {"owner0/repo0"}
        assert _match_names(conn, outside) == set()
        stored = conn.execute(
            "SELECT length(readme) FROM search_fts WHERE full_name='owner0/repo0'"
        ).fetchone()[0]
        assert stored == db.FTS_README_MAX_CHARS
    finally:
        conn.close()


def test_undecodable_readme_falls_back_to_empty(sandbox):
    write_source_files(sandbox)
    (sandbox["readmes"] / "owner0__repo0.md").write_bytes(b"\xff\xfe\x00bad-utf8")
    conn = db.rebuild()  # 解码失败按空串处理,不得让 rebuild 失败
    try:
        assert _readme_of(conn, "owner0/repo0") == ""
        assert _match_names(conn, "desc")  # 其余列检索不受影响
    finally:
        conn.close()
