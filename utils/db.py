"""Bot全体で共有する PostgreSQL 接続プール。

以前は各 Cog が個別に asyncpg.create_pool() を呼んでいたため、接続数が無駄に増えていました。
今後は get_pool(bot) で共有プールを取得してください（bot.db_pool として保持されます）。

注意: 共有プールなので、各 Cog の cog_unload() で pool.close() を呼ばないでください。
      プールは MyBot.close() で一度だけ閉じられます。
"""
from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

logger = logging.getLogger(__name__)

_lock: asyncio.Lock | None = None


async def get_pool(bot) -> asyncpg.Pool:
    """共有プールを返す（未作成ならここで作成する）"""
    global _lock
    pool = getattr(bot, "db_pool", None)
    if pool is not None:
        return pool

    if _lock is None:
        _lock = asyncio.Lock()

    async with _lock:
        pool = getattr(bot, "db_pool", None)
        if pool is not None:
            return pool

        url = os.getenv("DATABASE_URL3")
        if not url:
            raise ValueError("環境変数 'DATABASE_URL3' が設定されていません。")

        # statement_cache_size=0: pgbouncer / Neon の pooled 接続でも動くようにする
        pool = await asyncpg.create_pool(url, statement_cache_size=0)
        bot.db_pool = pool
        logger.info("[DB] 共有接続プールを作成しました。")
        return pool
