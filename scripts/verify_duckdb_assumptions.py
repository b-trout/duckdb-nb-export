"""設計ドキュメントの前提を実機検証するスクリプト。

design doc 6.3節「実機検証済み事項」の根拠。DuckDBバージョン更新時に
前提が変わっていないかを確認する(将来はpytestへ移植する — design doc 8章)。

実行方法:
    uv run --with duckdb python scripts/verify_duckdb_assumptions.py
"""

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading

import duckdb


def main() -> None:
    print(f"duckdb {duckdb.__version__}")
    tmp = tempfile.mkdtemp()

    # --- 1. SAVEPOINT はサポートされているか(6.3#1) ---
    con = duckdb.connect()
    con.execute("BEGIN")
    try:
        con.execute("SAVEPOINT sp1")
        print("1. SAVEPOINT: supported")
    except Exception as e:
        print(f"1. SAVEPOINT: NOT supported -> {type(e).__name__}")
    con.close()

    # --- 2. DDL は ROLLBACK で巻き戻るか(6.3#2) ---
    con = duckdb.connect()
    con.execute("CREATE TABLE keepme(i INT)")
    con.execute("BEGIN")
    con.execute("CREATE TABLE newtbl(i INT)")
    con.execute("DROP TABLE keepme")
    con.execute("ROLLBACK")
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    print(f"2. DDL rollback: tables={tables} (expect ['keepme'])")
    con.close()

    # --- 3. トランザクション内エラー後の継続可否(6.3#3) ---
    con = duckdb.connect()
    con.execute("BEGIN")
    try:
        con.execute("SELECT * FROM no_such_table")
    except Exception as e:
        print(f"3a. catalog error: {type(e).__name__}")
    try:
        con.execute("SELECT 42").fetchall()
        print("3b. continue after catalog error: OK")
    except Exception as e:
        print(f"3b. continue after catalog error: FAILED -> {type(e).__name__}")
    try:
        con.execute("CREATE TABLE u(i INT PRIMARY KEY)")
        con.execute("INSERT INTO u VALUES (1)")
        con.execute("INSERT INTO u VALUES (1)")
    except Exception as e:
        print(f"3c. constraint error: {type(e).__name__}")
    try:
        con.execute("SELECT 1").fetchall()
        print("3d. continue after constraint error: OK")
    except Exception as e:
        print(f"3d. continue after constraint error: ABORTED -> {type(e).__name__}")
    con.close()

    # --- 4. RW接続保持中のDBファイルへの別プロセスアクセス(6.3#4) ---
    db = os.path.join(tmp, "data.db")
    holder = duckdb.connect(db)
    holder.execute("CREATE TABLE x AS SELECT 1 AS a")
    child = textwrap.dedent(
        f"""
        import duckdb
        for label, kw in [("read_only", dict(read_only=True)), ("read_write", dict())]:
            try:
                duckdb.connect(r"{db}", **kw).close()
                print(f"4. cross-process connect ({{label}}): OK")
            except Exception as e:
                name = type(e).__name__
                print(f"4. cross-process connect ({{label}}): FAILED -> {{name}}")
        """
    )
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child], capture_output=True, text=True
    )
    print(r.stdout.strip())

    # --- 5. ロック保持中のOSコピーとWALの罠(6.3#5) ---
    cp = os.path.join(tmp, "copy.db")
    shutil.copy(db, cp)  # 本体のみコピー(WALを含めない)
    c = duckdb.connect(cp, read_only=True)
    copied_tables = c.execute("SHOW TABLES").fetchall()
    c.close()
    wal = os.path.exists(db + ".wal")
    print(
        f"5. copy-without-wal tables={copied_tables} / wal exists={wal}"
        " (直近変更がWALのみにある場合、本体コピーからは見えない)"
    )
    holder.close()

    # --- 6. トランザクション内の COPY TO は ROLLBACK で消えるか(6.3#6) ---
    con = duckdb.connect()
    out = os.path.join(tmp, "leak.csv")
    con.execute("BEGIN")
    con.execute(f"COPY (SELECT 1 AS a) TO '{out}' (FORMAT CSV)")
    con.execute("ROLLBACK")
    print(f"6. COPY TO survives rollback: file exists={os.path.exists(out)}")
    con.close()

    # --- 7. interrupt() によるタイムアウト実装可否(6.3#7) ---
    con = duckdb.connect()
    t = threading.Timer(0.5, con.interrupt)
    t.start()
    try:
        con.execute(
            "SELECT count(*) FROM range(10000000000) a, range(100) b"
        ).fetchall()
        print("7. interrupt: query finished (unexpected)")
    except Exception as e:
        print(f"7. interrupt: {type(e).__name__}")
    t.cancel()
    try:
        con.execute("SELECT 1").fetchall()
        print("7b. connection usable after interrupt: OK")
    except Exception as e:
        print(f"7b. connection usable after interrupt: FAILED -> {type(e).__name__}")
    con.close()

    # --- 8. トランザクション内の ATTACH / CHECKPOINT / SET / BEGIN(6.3#8) ---
    con = duckdb.connect()
    db2 = os.path.join(tmp, "other.db")
    for stmt in [f"ATTACH '{db2}' AS other", "CHECKPOINT", "SET threads=2", "BEGIN"]:
        try:
            con.execute("BEGIN")
            con.execute(stmt)
            con.execute("ROLLBACK")
            print(f"8. '{stmt.split()[0]}' inside txn: OK")
        except Exception as e:
            with contextlib.suppress(Exception):
                con.execute("ROLLBACK")
            print(f"8. '{stmt.split()[0]}' inside txn: FAILED -> {type(e).__name__}")
    con.close()

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
