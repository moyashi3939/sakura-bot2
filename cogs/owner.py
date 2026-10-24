"""ボットオーナー専用の管理コマンド (!skr_xxx)。

すべて「ボットオーナー」だけが実行できます。他のユーザーが実行しても無反応です。
オーナー = Discord開発者ポータルのアプリ所有者（Team の場合はメンバー）+ .env の OWNER_IDS。
"""
import asyncio
import datetime
import logging
import os
import platform
import sys

import discord
from discord.ext import commands

from utils.db import get_pool
from utils.perms import prefix_owner_only

logger = logging.getLogger(__name__)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS bot_blacklist (
    target_type TEXT NOT NULL,          -- 'user' または 'guild'
    target_id BIGINT NOT NULL,
    reason TEXT,
    added_by BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (target_type, target_id)
);
"""

NO_MENTIONS = discord.AllowedMentions.none()


def _uptime(delta: datetime.timedelta) -> str:
    total = int(delta.total_seconds())
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}日 {h}時間 {m}分 {s}秒"


def _ext_name(name: str) -> str:
    name = name.strip()
    if name.endswith(".py"):
        name = name[:-3]
    return name if name.startswith("cogs.") else f"cogs.{name}"


class OwnerCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pool = None

    async def cog_load(self):
        self.pool = await get_pool(self.bot)
        async with self.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)
            rows = await conn.fetch("SELECT target_type, target_id, reason FROM bot_blacklist")
        self.bot.blacklist_users = {r["target_id"]: r["reason"] or "" for r in rows if r["target_type"] == "user"}
        self.bot.blacklist_guilds = {r["target_id"]: r["reason"] or "" for r in rows if r["target_type"] == "guild"}
        logger.info("ブラックリスト読み込み: ユーザー%d / サーバー%d",
                    len(self.bot.blacklist_users), len(self.bot.blacklist_guilds))

    async def _confirm(self, ctx: commands.Context, text: str, timeout: float = 20.0) -> bool:
        await ctx.send(f"⚠️ {text}\n実行するには {int(timeout)}秒以内に `yes` と返信してください。")

        def check(m: discord.Message):
            return (
                m.author.id == ctx.author.id
                and m.channel.id == ctx.channel.id
                and m.content.lower() in ("yes", "y", "no", "n")
            )

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            await ctx.send("⌛ タイムアウトしたため中止しました。")
            return False
        if reply.content.lower() in ("yes", "y"):
            return True
        await ctx.send("中止しました。")
        return False

    # ==================================================
    # 🧩 Cog管理
    # ==================================================
    @commands.command(name="cogs", extras={"access": "owner"})
    @prefix_owner_only()
    async def cogs_list(self, ctx):
        """【オーナー】読み込み中 / 未読み込みの Cog 一覧を表示します"""
        loaded = sorted(e for e in self.bot.extensions if e.startswith("cogs."))
        on_disk = sorted(
            f"cogs.{f[:-3]}" for f in os.listdir("./cogs") if f.endswith(".py") and not f.startswith("_")
        )
        not_loaded = [e for e in on_disk if e not in loaded]
        embed = discord.Embed(title="🧩 Cog 一覧", color=discord.Color.blue())
        embed.add_field(name=f"✅ 読み込み中 ({len(loaded)})", value="\n".join(f"`{e}`" for e in loaded) or "なし", inline=False)
        if not_loaded:
            embed.add_field(name="⛔ 未読み込み", value="\n".join(f"`{e}`" for e in not_loaded), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="reload", extras={"access": "owner"})
    @prefix_owner_only()
    async def reload_cog(self, ctx, name: str):
        """【オーナー】Cogを再読み込みします（例: !skr_reload rank / !skr_reload all）"""
        targets = sorted(e for e in self.bot.extensions if e.startswith("cogs.")) if name.lower() == "all" else [_ext_name(name)]
        results = []
        for ext in targets:
            try:
                await self.bot.reload_extension(ext)
                results.append(f"✅ `{ext}`")
            except commands.ExtensionNotLoaded:
                results.append(f"⚠️ `{ext}` は読み込まれていません（!skr_load を使用）")
            except Exception as e:
                logger.exception("reload失敗: %s", ext)
                results.append(f"❌ `{ext}`: {type(e).__name__}: {e}"[:300])
        await ctx.send("\n".join(results)[:1900] + "\n※スラッシュコマンドの定義を変えた場合は `!skr_sync` も実行してください。")

    @commands.command(name="load", extras={"access": "owner"})
    @prefix_owner_only()
    async def load_cog(self, ctx, name: str):
        """【オーナー】Cogを読み込みます"""
        ext = _ext_name(name)
        try:
            await self.bot.load_extension(ext)
            await ctx.send(f"✅ `{ext}` を読み込みました。")
        except Exception as e:
            logger.exception("load失敗: %s", ext)
            await ctx.send(f"❌ `{ext}`: {type(e).__name__}: {e}"[:1900])

    @commands.command(name="unload", extras={"access": "owner"})
    @prefix_owner_only()
    async def unload_cog(self, ctx, name: str):
        """【オーナー】Cogを無効化します（owner は無効化不可）"""
        ext = _ext_name(name)
        if ext == "cogs.owner":
            await ctx.send("❌ このCog自身は無効化できません。")
            return
        try:
            await self.bot.unload_extension(ext)
            await ctx.send(f"✅ `{ext}` を無効化しました。")
        except Exception as e:
            await ctx.send(f"❌ `{ext}`: {type(e).__name__}: {e}"[:1900])

    @commands.command(name="sync", extras={"access": "owner"})
    @prefix_owner_only()
    async def sync_commands(self, ctx):
        """【オーナー】スラッシュコマンドをDiscordと同期します"""
        message = await ctx.send("⚡ スラッシュコマンドを同期中...")
        try:
            synced = await self.bot.tree.sync()
            await message.edit(content=f"✅ {len(synced)}個のスラッシュコマンドをグローバル同期しました！\n（反映に数分〜最大1時間かかる場合があります）")
        except Exception as e:
            await message.edit(content=f"❌ 同期エラーが発生しました: {e}")

    # ==================================================
    # 📊 状態確認
    # ==================================================
    @commands.command(name="stats", extras={"access": "owner"})
    @prefix_owner_only()
    async def stats(self, ctx):
        """【オーナー】Botの稼働状況を表示します"""
        now = datetime.datetime.now(datetime.timezone.utc)
        embed = discord.Embed(title="📊 Bot 稼働状況", color=discord.Color.green())
        embed.add_field(name="稼働時間", value=_uptime(now - self.bot.started_at), inline=False)
        embed.add_field(name="レイテンシ", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        embed.add_field(name="サーバー数", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="総メンバー数", value=str(sum(g.member_count or 0 for g in self.bot.guilds)), inline=True)
        embed.add_field(name="Cog", value=f"{len([e for e in self.bot.extensions if e.startswith('cogs.')])}個", inline=True)
        embed.add_field(name="スラッシュコマンド", value=f"{len(self.bot.tree.get_commands())}個", inline=True)
        pool = self.bot.db_pool
        embed.add_field(
            name="DB接続",
            value=f"{pool.get_size()}接続（空き {pool.get_idle_size()}）" if pool else "未接続",
            inline=True,
        )
        embed.add_field(
            name="ブラックリスト",
            value=f"ユーザー {len(self.bot.blacklist_users)} / サーバー {len(self.bot.blacklist_guilds)}",
            inline=True,
        )
        embed.set_footer(text=f"Python {platform.python_version()} / discord.py {discord.__version__}")
        await ctx.send(embed=embed)

    @commands.command(name="dbping", extras={"access": "owner"})
    @prefix_owner_only()
    async def dbping(self, ctx):
        """【オーナー】データベースの応答を確認します"""
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except Exception as e:
            await ctx.send(f"❌ DBエラー: {type(e).__name__}: {e}")
            return
        await ctx.send(f"🟢 DB応答OK: {(loop.time() - start) * 1000:.0f} ms")

    @commands.command(name="log", extras={"access": "owner"})
    @prefix_owner_only()
    async def send_log(self, ctx, lines: int = 100):
        """【オーナー】ログファイルの末尾を送信します（既定100行 / 最大1000行）"""
        path = "logs/bot.log"
        if not os.path.exists(path):
            await ctx.send("❌ ログファイルがまだありません。")
            return
        lines = max(1, min(lines, 1000))
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
        import io
        data = io.BytesIO("".join(tail).encode("utf-8"))
        await ctx.send(f"📄 ログ末尾 {len(tail)} 行", file=discord.File(data, filename="bot_log_tail.txt"))

    @commands.command(name="shutdown", extras={"access": "owner"})
    @prefix_owner_only()
    async def shutdown(self, ctx):
        """【オーナー】Botを停止します"""
        if await self._confirm(ctx, "Botを停止します。"):
            await ctx.send("👋 停止します。")
            await self.bot.close()
            sys.exit(0)

    # ==================================================
    # 🏠 サーバー管理
    # ==================================================
    def _find_guild(self, guild_id: int) -> discord.Guild | None:
        return self.bot.get_guild(guild_id)

    @commands.command(name="guildinfo", extras={"access": "owner"})
    @prefix_owner_only()
    async def guildinfo(self, ctx, guild_id: int):
        """【オーナー】参加中サーバーの詳細を表示します"""
        g = self._find_guild(guild_id)
        if g is None:
            await ctx.send("❌ そのIDのサーバーには参加していません。")
            return
        embed = discord.Embed(title=f"🏠 {g.name}", color=discord.Color.blurple())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="ID", value=f"`{g.id}`", inline=True)
        embed.add_field(name="オーナー", value=f"<@{g.owner_id}> (`{g.owner_id}`)", inline=True)
        embed.add_field(name="メンバー", value=str(g.member_count), inline=True)
        embed.add_field(name="作成日", value=discord.utils.format_dt(g.created_at, "D"), inline=True)
        embed.add_field(name="Botの権限", value="管理者" if g.me.guild_permissions.administrator else "一般", inline=True)
        embed.add_field(name="ブラックリスト", value="登録済み" if g.id in self.bot.blacklist_guilds else "なし", inline=True)
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @commands.command(name="leave", extras={"access": "owner"})
    @prefix_owner_only()
    async def leave_guild(self, ctx, guild_id: int):
        """【オーナー】指定したサーバーからBotを退出させます"""
        g = self._find_guild(guild_id)
        if g is None:
            await ctx.send("❌ そのIDのサーバーには参加していません。")
            return
        if await self._confirm(ctx, f"**{g.name}** (`{g.id}`) から退出します。"):
            await g.leave()
            await ctx.send(f"👋 **{g.name}** から退出しました。")

    # ==================================================
    # 🚫 ブラックリスト
    # ==================================================
    @commands.group(name="blacklist", invoke_without_command=True, extras={"access": "owner"})
    @prefix_owner_only()
    async def blacklist(self, ctx):
        """【オーナー】Botの利用禁止リスト（add/remove/list）"""
        await ctx.send(
            "使い方:\n"
            "`!skr_blacklist add user <ID> [理由]` / `!skr_blacklist add guild <ID> [理由]`\n"
            "`!skr_blacklist remove user|guild <ID>`\n"
            "`!skr_blacklist list`\n"
            "※登録するとコマンドが使えなくなります（オーナーは対象外）。サーバーの場合はBotが自動退出します。"
        )

    @blacklist.command(name="add")
    @prefix_owner_only()
    async def blacklist_add(self, ctx, kind: str, target_id: int, *, reason: str = ""):
        kind = kind.lower()
        if kind not in ("user", "guild"):
            await ctx.send("❌ 種類は `user` か `guild` を指定してください。")
            return
        if kind == "user":
            if target_id == self.bot.user.id or await self.bot.is_owner(discord.Object(id=target_id)):
                await ctx.send("❌ Bot自身やボットオーナーは登録できません。")
                return
            self.bot.blacklist_users[target_id] = reason
        else:
            self.bot.blacklist_guilds[target_id] = reason

        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_blacklist (target_type, target_id, reason, added_by) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (target_type, target_id) DO UPDATE SET reason = EXCLUDED.reason",
                kind, target_id, reason, ctx.author.id,
            )
        await ctx.send(f"🚫 {kind} `{target_id}` をブラックリストに登録しました。")

        if kind == "guild" and (g := self.bot.get_guild(target_id)):
            await g.leave()
            await ctx.send(f"👋 参加中だった **{g.name}** から退出しました。")

    @blacklist.command(name="remove", aliases=["del"])
    @prefix_owner_only()
    async def blacklist_remove(self, ctx, kind: str, target_id: int):
        kind = kind.lower()
        if kind not in ("user", "guild"):
            await ctx.send("❌ 種類は `user` か `guild` を指定してください。")
            return
        (self.bot.blacklist_users if kind == "user" else self.bot.blacklist_guilds).pop(target_id, None)
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM bot_blacklist WHERE target_type = $1 AND target_id = $2", kind, target_id
            )
        await ctx.send("✅ 解除しました。" if result == "DELETE 1" else "ℹ️ 登録されていませんでした。")

    @blacklist.command(name="list")
    @prefix_owner_only()
    async def blacklist_list(self, ctx):
        users = "\n".join(f"<@{i}> (`{i}`) {r}" for i, r in self.bot.blacklist_users.items()) or "なし"
        guilds = "\n".join(f"`{i}` {r}" for i, r in self.bot.blacklist_guilds.items()) or "なし"
        embed = discord.Embed(title="🚫 ブラックリスト", color=discord.Color.dark_red())
        embed.add_field(name="ユーザー", value=users[:1000], inline=False)
        embed.add_field(name="サーバー", value=guilds[:1000], inline=False)
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)


async def setup(bot: commands.Bot):
    await bot.add_cog(OwnerCog(bot))
