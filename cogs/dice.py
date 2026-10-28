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

        # パターン1: チョイス機能 (例: choice[A,B] 焼肉か寿司)
        choice_match = re.match(r"^(s)?choice(?:\[|\s+)(.+?)(?:\])?(?:\s+(.+))?$", content, re.IGNORECASE)
        if choice_match:
            if not await self.get_enabled(message.guild.id):
                return
            is_secret = bool(choice_match.group(1))
            raw_options = choice_match.group(2)
            memo = choice_match.group(3)
            options = [opt.strip() for opt in re.split(r"[,/,\s]+", raw_options) if opt.strip()]
            if options:
                chosen = random.choice(options)
                memo_str = f" ({memo})" if memo else ""
                desc = f"**choice[{','.join(options)}]{memo_str}** ➔ **{chosen}**"
                await self._send_result(message, desc, is_secret, color=discord.Color.dark_embed())
            return

        # パターン2: CoC 6版 CCB / CC (例: CCB<=50 目星, CC<=65 組みつき)
        ccb_match = re.match(r"^(s)?ccb?(?:<=?(\d+))?(?:\s+(.+))?$", content, re.IGNORECASE)
        if ccb_match:
            if not await self.get_enabled(message.guild.id):
                return
            is_secret = bool(ccb_match.group(1))
            target_str = ccb_match.group(2)
            target = int(target_str) if target_str else None
            memo = ccb_match.group(3)

            val = random.randint(1, 100)
            desc, color = self._eval_coc6(val, target, memo)
            await self._send_result(message, desc, is_secret, color)
            return

        # パターン3: 通常のダイスロール (例: 1d100 目星, 2d6+3 ダメージ, s1d100)
        is_secret = False
        roll_content = content
        if roll_content.lower().startswith('s'):
            is_secret = True
            roll_content = roll_content[1:]

        dice_match = re.match(r"^(\d+)d(\d+)(?:([+-])(\d+))?(?:\s+(.+))?$", roll_content, re.IGNORECASE)
        if dice_match:
            if not await self.get_enabled(message.guild.id):
                return

            count = int(dice_match.group(1))
            sides = int(dice_match.group(2))
            op = dice_match.group(3)
            mod = int(dice_match.group(4)) if dice_match.group(4) else 0
            memo = dice_match.group(5)

            if count <= 0 or sides <= 0 or count > 50 or sides > 100000:
                return

            try:
                results = [random.randint(1, sides) for _ in range(count)]
                raw_sum = sum(results)

                if op == "+":
                    total = raw_sum + mod
                    mod_str = f"+{mod}"
                elif op == "-":
                    total = raw_sum - mod
                    mod_str = f"-{mod}"
                else:
                    total = raw_sum
                    mod_str = ""

                dice_expr = f"{count}D{sides}{mod_str}"
                memo_str = f" ({memo})" if memo else ""

                color = discord.Color.dark_embed()
                extra_res = ""

                # 💡 1d100単体（または修正付き1d100）の場合のクリファン判定
                if count == 1 and sides == 100 and not op:
                    if results[0] <= 5:
                        extra_res = " ➔ **決定的成功 (クリティカル)**"
                        color = discord.Color.gold()
                    elif results[0] >= 96:
                        extra_res = " ➔ **致命的失敗 (ファンブル)**"
                        color = discord.Color.dark_gray()

                if count > 1:
                    results_str = ", ".join(map(str, results))
                    if len(results_str) > 1800:
                        results_str = "出目多数につき省略"
                    desc = f"**{dice_expr}{memo_str}**\n[{results_str}]{mod_str} ➔ **{total}**{extra_res}"
                else:
                    desc = f"**{dice_expr}{memo_str}**\n[{results[0]}]{mod_str} ➔ **{total}**{extra_res}"

                await self._send_result(message, desc, is_secret, color)
            except Exception:
                pass

    def _eval_coc6(self, val: int, target: int | None, memo: str | None) -> tuple[str, discord.Color]:
        """CoC 6版の勝敗判定 logic (結果に応じたカラーを返す)"""
        memo_str = f" ({memo})" if memo else ""
        expr_str = "CCB" if target is None else f"CCB<={target}"
        
        if target is None:
            # 目標値なしのCCBの場合は単体1d100と同じ扱い
            color = discord.Color.dark_embed()
            res_str = f"**{expr_str}{memo_str}** ➔ **{val}**"
            if val <= 5:
                res_str += " ➔ **決定的成功 (クリティカル)**"
                color = discord.Color.gold()
            elif val >= 96:
                res_str += " ➔ **致命的失敗 (ファンブル)**"
                color = discord.Color.dark_gray()
            return res_str, color

        if val <= target:
            if val <= 5:
                res = "決定的成功 (クリティカル)"
                color = discord.Color.gold()
            elif val <= max(1, target // 5):
                res = "スペシャル"
                color = discord.Color.green()
            else:
                res = "成功"
                color = discord.Color.green()
        else:
            if val >= 96:
                res = "致命的失敗 (ファンブル)"
                color = discord.Color.dark_gray()
            else:
                res = "失敗"
                color = discord.Color.red()

        return f"**{expr_str}{memo_str}** ➔ [{val}] ➔ **{res}**", color

    async def _send_result(self, message: discord.Message, description: str, is_secret: bool, color: discord.Color):
        """結果の送信制御（シークレットダイスおよびカラー対応）"""
        embed = discord.Embed(
            description=description,
            color=color
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