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
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dice_settings (
                    guild_id BIGINT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
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

        content = message.content.strip()

        # パターン1: チョイス機能 (choice[A,B] や choice A B C)
        choice_match = re.match(r"^(s)?choice(?:\[|\s+)(.+?)(?:\])?$", content, re.IGNORECASE)
        if choice_match:
            if not await self.get_enabled(message.guild.id):
                return
            is_secret = bool(choice_match.group(1))
            raw_options = choice_match.group(2)
            options = [opt.strip() for opt in re.split(r"[,/,\s]+", raw_options) if opt.strip()]
            if options:
                chosen = random.choice(options)
                desc = f"**choice[{','.join(options)}]** ➔ **{chosen}**"
                await self._send_result(message, desc, is_secret)
            return

        # パターン2: CoC 6版 CCB / CC (例: CCB<=50, CC<=65)
        ccb_match = re.match(r"^(s)?ccb?(?:<=?(\d+))?$", content, re.IGNORECASE)
        if ccb_match:
            if not await self.get_enabled(message.guild.id):
                return
            is_secret = bool(ccb_match.group(1))
            target_str = ccb_match.group(2)
            target = int(target_str) if target_str else None

            val = random.randint(1, 100)
            desc = self._eval_coc6(val, target)
            await self._send_result(message, desc, is_secret)
            return

        # パターン3: 複数ダイス・加減算対応ロール (例: 1d4+1d3, 2d6+3, s1d100+5)
        # 先頭の 's' の有無をチェック
        is_secret = False
        roll_content = content
        if roll_content.lower().startswith('s'):
            is_secret = True
            roll_content = roll_content[1:]

        # ダイス式の簡易バリデーション（数字、d、+-のみで構成されているか）
        if re.match(r"^\d+d\d+(?:[+-]\d+d\d+|[+-]\d+)*$", roll_content, re.IGNORECASE):
            if not await self.get_enabled(message.guild.id):
                return

            try:
                desc = self._eval_complex_dice(roll_content)
                await self._send_result(message, desc, is_secret)
                return
            except Exception:
                pass  # パースや計算に失敗した場合は無視してスルー

    def _eval_complex_dice(self, expr: str) -> str:
        """複数ダイスや加減算が含まれる式を計算する"""
        # トークン（ダイスまたは定数）に分解して抽出
        tokens = re.findall(r"([+-]?)(?:(\d+)d(\d+)|(\d+))", expr, re.IGNORECASE)
        
        total = 0
        details = []
        
        for sign, count_str, sides_str, const_str in tokens:
            op = -1 if sign == "-" else 1
            
            if count_str and sides_str:
                count = int(count_str)
                sides = int(sides_str)
                if count <= 0 or sides <= 0 or count > 50 or sides > 100000:
                    raise ValueError("Invalid dice range")
                
                rolls = [random.randint(1, sides) for _ in range(count)]
                sub_sum = sum(rolls)
                total += op * sub_sum
                
                # 表示形式の調整
                sign_char = "-" if op == -1 else ("+" if details else "")
                rolls_repr = ",".join(map(str, rolls))
                if len(rolls_repr) > 50:
                    rolls_repr = "..."
                details.append(f"{sign_char}{count}d{sides}[{rolls_repr}]")
                
            elif const_str:
                val = int(const_str)
                total += op * val
                sign_char = "-" if op == -1 else ("+" if details else "")
                details.append(f"{sign_char}{val}")

        expr_cleaned = expr.upper()
        details_str = "".join(details)
        # 先頭の不要なプラスを取り除く
        if details_str.startswith("+"):
            details_str = details_str[1:]

        return f"**{expr_cleaned}**\n{details_str} ➔ **{total}**"

    def _eval_coc6(self, val: int, target: int | None) -> str:
        """CoC 6版の勝敗判定 logic"""
        expr_str = "CCB" if target is None else f"CCB<={target}"
        if target is None:
            return f"**{expr_str}** ➔ **{val}**"

        if val <= target:
            if val <= 5:
                res = "決定的成功 (クリティカル)"
            elif val <= max(1, target // 5):
                res = "スペシャル"
            else:
                res = "成功"
        else:
            if val >= 96:
                res = "致命的失敗 (ファンブル)"
            else:
                res = "失敗"

        return f"**{expr_str}** ➔ [{val}] ➔ **{res}**"

    async def _send_result(self, message: discord.Message, description: str, is_secret: bool):
        """結果の送信制御（シークレットダイス対応）"""
        embed = discord.Embed(
            description=description,
            color=discord.Color.dark_embed()
        )

        if is_secret:
            try:
                await message.author.send(f"🔒 **[シークレットダイス]** ({message.channel.mention})\n" + description)
                notice_embed = discord.Embed(
                    description=f"🔒 {message.author.mention} がシークレットダイスを振りました。",
                    color=discord.Color.dark_gray()
                )
                await message.reply(embed=notice_embed, mention_author=False)
            except discord.Forbidden:
                await message.reply("❌ DMがブロックされているためシークレットダイスを送信できませんでした。", delete_after=10)
        else:
            await message.reply(embed=embed, mention_author=False)


async def setup(bot):
    await bot.add_cog(Dice(bot))
