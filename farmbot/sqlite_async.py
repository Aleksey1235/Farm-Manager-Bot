from __future__ import annotations

import asyncio
import sqlite3


class AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    async def fetchone(self):
        return await asyncio.to_thread(self._cursor.fetchone)

    async def fetchall(self):
        return await asyncio.to_thread(self._cursor.fetchall)


class AsyncConnection:
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()

    async def execute(self, sql, params=()):
        async with self._lock:
            cur = await asyncio.to_thread(self._conn.execute, sql, params)
            return AsyncCursor(cur)

    async def executescript(self, script):
        async with self._lock:
            await asyncio.to_thread(self._conn.executescript, script)

    async def commit(self):
        async with self._lock:
            await asyncio.to_thread(self._conn.commit)

    async def close(self):
        async with self._lock:
            await asyncio.to_thread(self._conn.close)
