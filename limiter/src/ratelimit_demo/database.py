import asyncio
import ssl
from typing import Any

import aiomysql

from .config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: aiomysql.Pool | None = None
        self.lock = asyncio.Lock()

    def tls_context(self) -> ssl.SSLContext | None:
        config = self.settings
        if not config.database_tls:
            return None
        if not config.database_tls_verify_identity and not config.database_ca:
            raise ValueError(
                "An explicit ProxySQL CA is required when hostname verification is disabled"
            )
        context = ssl.create_default_context(cafile=config.database_ca)
        context.check_hostname = config.database_tls_verify_identity
        return context

    async def get_pool(self) -> aiomysql.Pool:
        async with self.lock:
            if self.pool is None:
                config = self.settings
                tls = self.tls_context()
                self.pool = await aiomysql.create_pool(
                    host=config.database_host,
                    port=config.database_port,
                    user=config.database_user,
                    password=config.database_password,
                    db=config.database_name,
                    ssl=tls,
                    minsize=0,
                    maxsize=16,
                    connect_timeout=2,
                    autocommit=True,
                    pool_recycle=60,
                )
            return self.pool

    async def query(self, query_id: str, customer_id: int) -> list[dict[str, Any]]:
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(
                        "SELECT customer_id, name FROM customers WHERE customer_id=%s",
                        (customer_id,),
                    )
                    rows = list(await cursor.fetchall())
                    await cursor.execute(
                        "INSERT INTO query_audit (query_id) VALUES (%s)", (query_id,)
                    )
                    return rows
            except BaseException:
                # A cancelled protocol exchange must never return a dirty socket to the pool.
                conn.close()
                raise

    async def health(self) -> dict[str, bool]:
        proxysql = False
        mysql = False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.settings.database_host, self.settings.database_port),
                timeout=1,
            )
            writer.close()
            await writer.wait_closed()
            proxysql = True
            async with asyncio.timeout(2):
                pool = await self.get_pool()
                async with pool.acquire() as conn:
                    try:
                        async with conn.cursor() as cursor:
                            await cursor.execute("SELECT 1")
                            mysql = (await cursor.fetchone())[0] == 1
                    except BaseException:
                        conn.close()
                        raise
        except (OSError, TimeoutError, aiomysql.Error):
            pass
        return {"proxysql": proxysql, "mysql": mysql}

    async def close(self) -> None:
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
