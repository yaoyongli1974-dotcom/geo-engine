"""SQLite 存储层 —— 内容资产、发布产物、探测结果的持久化。

用标准库 sqlite3，按业务线分区（所有表都带 business_line 字段），
支持内容哈希去重与增量更新。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL,
    title TEXT,
    source_uri TEXT,
    source_type TEXT,
    authority INTEGER DEFAULT 2,
    checksum TEXT,
    payload TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_bl ON documents(business_line);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL,
    doc_id TEXT,
    heading_path TEXT,
    score REAL DEFAULT 0,
    payload TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunk_bl ON chunks(business_line);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL,
    topic TEXT,
    claim TEXT,
    score REAL DEFAULT 0,
    payload TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fact_bl ON facts(business_line);

CREATE TABLE IF NOT EXISTS qas (
    id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL,
    question TEXT,
    intent TEXT,
    score REAL DEFAULT 0,
    payload TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_qa_bl ON qas(business_line);

CREATE TABLE IF NOT EXISTS terms (
    id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL,
    term TEXT,
    payload TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_term_bl ON terms(business_line);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL,
    path TEXT,
    format TEXT,
    checksum TEXT,
    published INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_path ON artifacts(business_line, path);

CREATE TABLE IF NOT EXISTS probes (
    id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL,
    engine TEXT,
    query TEXT,
    mentioned INTEGER DEFAULT 0,
    cited INTEGER DEFAULT 0,
    rank INTEGER DEFAULT 0,
    payload TEXT,
    checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_bl ON probes(business_line);
CREATE INDEX IF NOT EXISTS idx_probe_time ON probes(checked_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_line TEXT,
    stage TEXT,
    status TEXT,
    stats TEXT,
    started_at TEXT,
    finished_at TEXT
);
"""


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------------------------------------------------------------- 基础
    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def stats(self, business_line: Optional[str] = None) -> Dict[str, int]:
        tables = ["documents", "chunks", "facts", "qas", "terms", "artifacts", "probes"]
        out: Dict[str, int] = {}
        with self._lock:
            for t in tables:
                if business_line:
                    cur = self._conn.execute(
                        f"SELECT COUNT(*) c FROM {t} WHERE business_line=?", (business_line,)
                    )
                else:
                    cur = self._conn.execute(f"SELECT COUNT(*) c FROM {t}")
                out[t] = int(cur.fetchone()["c"])
        return out

    # ---------------------------------------------------------------- 写入
    def upsert_many(self, table: str, rows: Iterable[Dict[str, Any]]) -> int:
        """通用 upsert：rows 中每个 dict 的键即列名，payload 自动 json 化。"""
        rows = list(rows)
        if not rows:
            return 0
        cols = sorted({k for r in rows for k in r})
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        data = []
        for r in rows:
            row = []
            for c in cols:
                v = r.get(c)
                row.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            data.append(row)
        with self.tx() as conn:
            conn.executemany(sql, data)
        return len(rows)

    def save_documents(self, docs) -> int:
        return self.upsert_many("documents", [
            {
                "id": d.id, "business_line": d.business_line, "title": d.title,
                "source_uri": d.source_uri, "source_type": d.source_type,
                "authority": d.authority, "checksum": _cs(d.content),
                "payload": d.to_dict(), "updated_at": d.updated_at,
            } for d in docs
        ])

    def save_chunks(self, chunks) -> int:
        return self.upsert_many("chunks", [
            {
                "id": c.id, "business_line": c.business_line, "doc_id": c.doc_id,
                "heading_path": c.heading_path, "score": c.score,
                "payload": c.to_dict(), "updated_at": c.updated_at,
            } for c in chunks
        ])

    def save_facts(self, facts) -> int:
        return self.upsert_many("facts", [
            {
                "id": f.id, "business_line": f.business_line, "topic": f.topic,
                "claim": f.claim, "score": f.score,
                "payload": f.to_dict(), "updated_at": f.updated_at,
            } for f in facts
        ])

    def save_qas(self, qas) -> int:
        return self.upsert_many("qas", [
            {
                "id": q.id, "business_line": q.business_line, "question": q.question,
                "intent": q.intent, "score": q.score,
                "payload": q.to_dict(), "updated_at": q.updated_at,
            } for q in qas
        ])

    def save_terms(self, terms) -> int:
        return self.upsert_many("terms", [
            {"id": t.id, "business_line": t.business_line, "term": t.term,
             "payload": t.to_dict(), "updated_at": t.updated_at}
            for t in terms
        ])

    def save_probes(self, probes) -> int:
        return self.upsert_many("probes", [
            {
                "id": p.id, "business_line": p.business_line, "engine": p.engine,
                "query": p.query, "mentioned": int(p.mentioned), "cited": int(p.cited),
                "rank": p.rank, "payload": p.to_dict(), "checked_at": p.checked_at,
            } for p in probes
        ])

    # ---------------------------------------------------------------- 读取
    def load_facts(self, business_line: str, min_score: float = 0.0) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM facts WHERE business_line=? AND score>=? ORDER BY score DESC",
                (business_line, min_score),
            )
            return [json.loads(r["payload"]) for r in cur.fetchall()]

    def load_qas(self, business_line: str, min_score: float = 0.0) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM qas WHERE business_line=? AND score>=? ORDER BY score DESC",
                (business_line, min_score),
            )
            return [json.loads(r["payload"]) for r in cur.fetchall()]

    def load_terms(self, business_line: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM terms WHERE business_line=? ORDER BY term", (business_line,)
            )
            return [json.loads(r["payload"]) for r in cur.fetchall()]

    def load_chunks(self, business_line: str, min_score: float = 0.0) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM chunks WHERE business_line=? AND score>=? ORDER BY score DESC",
                (business_line, min_score),
            )
            return [json.loads(r["payload"]) for r in cur.fetchall()]

    def load_probes(self, business_line: str, since: Optional[str] = None,
                    limit: int = 5000) -> List[Dict[str, Any]]:
        sql = "SELECT payload FROM probes WHERE business_line=?"
        args: List[Any] = [business_line]
        if since:
            sql += " AND checked_at>=?"
            args.append(since)
        sql += " ORDER BY checked_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self._conn.execute(sql, args)
            return [json.loads(r["payload"]) for r in cur.fetchall()]

    # ---------------------------------------------------------------- 产物 / 增量
    def artifact_checksums(self, business_line: str) -> Dict[str, str]:
        """返回 {path: checksum}，用于增量发布判定。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT path, checksum FROM artifacts WHERE business_line=?", (business_line,)
            )
            return {r["path"]: r["checksum"] for r in cur.fetchall()}

    def mark_artifacts(self, business_line: str, items: List[Dict[str, Any]]) -> None:
        rows = [{
            "id": f"{business_line}:{i['path']}",
            "business_line": business_line,
            "path": i["path"],
            "format": i.get("format", "text"),
            "checksum": i["checksum"],
            "published": 1,
            "updated_at": i.get("updated_at", ""),
        } for i in items]
        self.upsert_many("artifacts", rows)

    # ---------------------------------------------------------------- 运行日志
    def log_run(self, business_line: str, stage: str, status: str,
                stats: Dict[str, Any], started_at: str, finished_at: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO runs (business_line, stage, status, stats, started_at, finished_at)"
                " VALUES (?,?,?,?,?,?)",
                (business_line, stage, status, json.dumps(stats, ensure_ascii=False),
                 started_at, finished_at),
            )

    def recent_runs(self, business_line: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM runs WHERE business_line=? ORDER BY id DESC LIMIT ?",
                (business_line, limit),
            )
            return [dict(r) for r in cur.fetchall()]


def _cs(text: str) -> str:
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()
