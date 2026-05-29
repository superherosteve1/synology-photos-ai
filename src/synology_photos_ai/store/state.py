from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ProcessState:
    photo_id: int
    filename: str
    description: str
    tags: list[str]
    processed_at: str
    model: str


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed (
                photo_id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL,
                description TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                model TEXT NOT NULL,
                processed_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def is_processed(self, photo_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        return row is not None

    def mark_processed(self, state: ProcessState) -> None:
        self._conn.execute(
            """
            INSERT INTO processed (photo_id, filename, description, tags_json, model, processed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(photo_id) DO UPDATE SET
                filename = excluded.filename,
                description = excluded.description,
                tags_json = excluded.tags_json,
                model = excluded.model,
                processed_at = excluded.processed_at
            """,
            (
                state.photo_id,
                state.filename,
                state.description,
                json.dumps(state.tags),
                state.model,
                state.processed_at,
            ),
        )
        self._conn.commit()

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM processed").fetchone()
        return int(row[0]) if row else 0

    def recent(self, limit: int = 20) -> list[ProcessState]:
        rows = self._conn.execute(
            """
            SELECT photo_id, filename, description, tags_json, model, processed_at
            FROM processed
            ORDER BY processed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result: list[ProcessState] = []
        for photo_id, filename, description, tags_json, model, processed_at in rows:
            result.append(
                ProcessState(
                    photo_id=photo_id,
                    filename=filename,
                    description=description,
                    tags=json.loads(tags_json),
                    processed_at=processed_at,
                    model=model,
                )
            )
        return result

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
