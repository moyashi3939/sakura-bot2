import random
import re

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool
from utils.perms import admin_or_owner


class Dice(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.pool: asyncpg.Pool | None = None
        # {guild_id: enabled} メッセージのたびにDBへ問い合わせないためのキャッシュ
        self._cache: dict[int, bool] = {}

    async def cog_load(self):
        """共有プールを取得し、専用テーブルを初期化する"""
        self.pool = await get_pool(self.bot)

        async with self.pool.acquire() as connection:
            # 以前は setting.py と同じ guild_settings テーブルを共用していたが、
            # 列定義が衝突するため専用テーブルに分離した
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dice_settings (
                    guild_id BIGINT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            # 旧テーブルに保存済みの設定を一度だけ引き継ぐ（既にある設定は上書きしない）
            has_old_column = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'guild_settings' AND column_name = 'enabled'
                )
                """
            )
            if has_old_column:
                await connection.execute(
                    """
                    INSERT INTO dice_settings (guild_id, enabled)
                    SELECT guild_id, enabled FROM guild_settings WHERE enabled IS NOT NULL
                    ON CONFLICT (guild_id) DO NOTHING
                    """
                )

    async def get_enabled(self, guild_id: int) -> bool:
        """サーバーのダイス設定を取得する（デフォルトはFalse）"""
        if guild_id in self._cache:
            return self._cache[guild_id]
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT enabled FROM dice_settings WHERE guild_id = $1", guild_id
            )
        enabled = bool(row["enabled"]) if row else False
        self._cache[guild_id] = enabled
        return enabled

    async def set_enabled(self, guild_id: int, enabled: bool):
        """サーバーのダイス設定を保存する（UPSERT）"""
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO dice_settings (guild_id, enabled)
                VALUES ($1, $2)
                ON CONFLICT (guild_id)
                DO UPDATE SET enabled = EXCLUDED.enabled
                """,
                guild_id, enabled
            )
        self._cache[guild_id] = enabled

    @app_commands.command(name="roll", description="このサーバーのダイス機能の有効/無効を切り替えます")
    @app_commands.describe(status="有効にするか無効にするかを選択してください")
    @app_commands.choices(status=[
        app_commands.Choice(name="有効", value="enable"),
        app_commands.Choice(name="無効", value="disable"),
    ])
    @app_commands.guild_only()
    @admin_or_owner()
    async def roll_setting(self, interaction: discord.Interaction, status: str):
        guild_id = interaction.guild_id

        if status == "enable":
            await self.set_enabled(guild_id, True)
            await interaction.response.send_message("✅ このサーバーでダイス機能を**有効**にしました", ephemeral=True)
        else:
            await self.set_enabled(guild_id, False)
            await interaction.response.send_message("❌ このサーバーでダイス機能を**無効**にしました", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # 先に軽いパターン判定をして、無関係なメッセージではDBを見に行かない
        match = re.search(r"\b(\d+)d(\d+)\b", message.content.lower())
        if not match:
            return

        if not await self.get_enabled(message.guild.id):
            return

        count = int(match.group(1))
        sides = int(match.group(2))
        dice_str = f"{count}d{sides}"

        if count <= 0 or sides <= 0 or count > 100 or sides > 100000:
            return

        results = [random.randint(1, sides) for _ in range(count)]
        total = sum(results)

        if count > 1:
            results_str = ", ".join(map(str, results))
            if len(results_str) > 1900:
                results_str = " (出目が多すぎるため省略されました) "
            description = f"**{dice_str}**\n[{results_str}] > {total}"
        else:
            description = f"**{dice_str}**\n[{results[0]}] > {total}"

        embed = discord.Embed(
            description=description,
            color=discord.Color.dark_embed()
        )

        await message.reply(embed=embed, mention_author=False)


async def setup(bot):
    await bot.add_cog(Dice(bot))
