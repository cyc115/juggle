#!/usr/bin/env python3
"""Research KB — SQLite DB layer with sqlite-vec + FTS5 hybrid search."""
import sqlite3
import struct
from pathlib import Path
from typing import Optional


def _serialize_f32(values: list[float]) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


class ResearchKB:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser())

    def _connect(self) -> sqlite3.Connection:
        import sqlite_vec
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS articles (
                    id      INTEGER PRIMARY KEY,
                    title   TEXT NOT NULL,
                    url     TEXT UNIQUE NOT NULL,
                    score   INTEGER,
                    date    TEXT,
                    source  TEXT NOT NULL,
                    summary TEXT,
                    body    TEXT
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS articles_vec USING vec0(
                    article_id INTEGER PRIMARY KEY,
                    embedding  FLOAT[1536]
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
                    title, summary,
                    content=articles, content_rowid=id
                );

                CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
                    INSERT INTO articles_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
                END;

                CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
                    INSERT INTO articles_fts(articles_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
                END;

                CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
                    INSERT INTO articles_fts(articles_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
                    INSERT INTO articles_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
                END;

                CREATE TABLE IF NOT EXISTS pdf_files (
                    path         TEXT PRIMARY KEY,
                    mtime        REAL NOT NULL,
                    ingested_at  TEXT NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def insert_article(
        self, title: str, url: str, score: Optional[int], date: Optional[str],
        source: str, summary: Optional[str], body: Optional[str],
    ) -> Optional[int]:
        """Insert article; skip if URL exists. Returns new row id or None."""
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles (title, url, score, date, source, summary, body)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (title, url, score, date, source, summary, body),
            )
            conn.commit()
            return cur.lastrowid if cur.rowcount else None
        finally:
            conn.close()

    def upsert_embedding(self, article_id: int, embedding: list[float]) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM articles_vec WHERE article_id=?", (article_id,))
            conn.execute(
                "INSERT INTO articles_vec (article_id, embedding) VALUES (?, ?)",
                (article_id, _serialize_f32(embedding)),
            )
            conn.commit()
        finally:
            conn.close()

    def get_article_id(self, url: str) -> Optional[int]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def fts_search(self, query: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT a.id, a.title, a.url, a.score, a.date, a.source, a.summary,
                          row_number() OVER (ORDER BY f.rank) AS rnk
                   FROM articles_fts f
                   JOIN articles a ON a.id = f.rowid
                   WHERE articles_fts MATCH ?
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def hybrid_search(self, query_embedding: list[float], query_text: str, k: int = 10) -> list[dict]:
        """RRF fusion of vec0 KNN and FTS5. Returns top-k results."""
        conn = self._connect()
        try:
            blob = _serialize_f32(query_embedding)
            rows = conn.execute(
                """
                WITH vec_hits AS (
                    SELECT article_id AS id,
                           row_number() OVER (ORDER BY distance) AS rnk
                    FROM articles_vec
                    WHERE embedding MATCH ? AND k = 20
                ),
                fts_hits AS (
                    SELECT rowid AS id,
                           row_number() OVER (ORDER BY rank) AS rnk
                    FROM articles_fts
                    WHERE articles_fts MATCH ?
                    LIMIT 20
                ),
                uniq AS (
                    SELECT id FROM vec_hits
                    UNION
                    SELECT id FROM fts_hits
                )
                SELECT a.id, a.title, a.url, a.score, a.date, a.source, a.summary,
                       (1.0/(60 + COALESCE(v.rnk, 60)) + 1.0/(60 + COALESCE(f.rnk, 60))) AS rrf
                FROM uniq
                JOIN articles a ON a.id = uniq.id
                LEFT JOIN vec_hits v ON v.id = uniq.id
                LEFT JOIN fts_hits f ON f.id = uniq.id
                ORDER BY rrf DESC
                LIMIT ?
                """,
                [blob, query_text, k],
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def is_pdf_ingested(self, path: str, mtime: float) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT mtime FROM pdf_files WHERE path=?", (path,)
            ).fetchone()
            return row is not None and abs(row["mtime"] - mtime) < 0.01
        finally:
            conn.close()

    def mark_pdf_ingested(self, path: str, mtime: float) -> None:
        from datetime import datetime, timezone
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO pdf_files (path, mtime, ingested_at) VALUES (?, ?, ?)",
                (path, mtime, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
