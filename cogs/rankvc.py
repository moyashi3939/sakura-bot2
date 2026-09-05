import asyncio
from datetime import datetime, timezone
import logging
import os
import random
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# VC専用のランク定義（テキスト版とは別個にカスタマイズ可能）
VC_RANKS = {
    500: ("🎙️👑 Voice Master (声の神)", discord.Color.from_rgb(255, 100, 100)),
    300: ("🎙️💎 Sound Legend (音の伝説)", discord.Color.from_rgb(255, 215, 0)),
    150: ("🎙️⭐ Talker Elite (エリート通話者)", discord.Color.from_rgb(138, 43, 226)),
    50: ("🎙️🎧 Active Speaker (アクティブ)", discord.Color.from_rgb(0, 191, 255)),
    1: ("🎙️🌱 Voice Novice (ビギナー)", discord.Color.from_rgb(50, 205, 50)),
}


class IndependentVoiceLevelCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_pool: Optional["asyncpg.Pool"] = None
        # {(guild_id, user_id): 入室時刻}
        self.active_vc_sessions: dict[tuple[int, int], datetime] = {}

    async def cog_load(self):
        """完全独立した専用テーブルの作成と既存VCセッションの捕捉"""
        import asyncpg

        db_url = os.getenv("DATABASE_URL3")
        if not db_url:
            raise ValueError(
                "環境変数 'DATABASE_URL3' が設定されていません。"
            )

        self.db_pool = await asyncpg.create_pool(db_url)

        async with self.db_pool.acquire() as conn:
            # 完全に独立したテーブル（vc_user_levels）
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vc_user_levels (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    vc_xp INT DEFAULT 0,
                    vc_level INT DEFAULT 1,
                    last_active_at TIMESTAMP WITH TIME ZONE,
                    PRIMARY KEY (guild_id, user_id)
                );
            """
            )

        # 起動時にすでにVCにいるユーザーをトラッキング開始
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    # 1人のみ、あるいは耳コピ/ミュート状態の場合は除外
                    if len(vc.members) == 1 or member.voice.self_deaf or member.voice.deaf:
                        continue
                    self.active_vc_sessions[(guild.id, member.id)] = now

        # 定期保存ループの起動（3分おき）
        self.save_vc_progress.start()
        logger.info("[IndependentVoiceLevelCog] 独立型VCレベリングシステムの初期化が完了しました。")

    async def cog_unload(self):
        """アンロード時に定期ループを停止し、残ったセッションをすべてセーブ"""
        self.save_vc_progress.cancel()
        await self._flush_all_sessions(force_save=True)
        if self.db_pool:
            await self.db_pool.close()

    def _calc_level(self, xp: int) -> int:
        """VC専用の独自計算式（XPが緩やかに必要になる仕様）"""
        return int((xp / 60) ** (1 / 1.4)) + 1

    def _get_vc_rank(self, level: int) -> tuple[str, discord.Color]:
        """VC専用のランク名とカラーを返す"""
        for req_lvl, (r_name, r_color) in VC_RANKS.items():
            if level >= req_lvl:
                return r_name, r_color
        return "🎙️🌱 Voice Novice (ビギナー)", discord.Color.from_rgb(50, 205, 50)

    async def _add_vc_xp(self, guild_id: int, user_id: int, earned_xp: int, member: Optional[discord.Member] = None):
        """データベースへのXP加算とレベルアップ時の処理"""
        if not self.db_pool or earned_xp <= 0:
            return

        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT vc_xp, vc_level FROM vc_user_levels 
                    WHERE guild_id = $1 AND user_id = $2
                """,
                    guild_id,
                    user_id,
                )

                current_xp = row["vc_xp"] if row else 0
                current_level = row["vc_level"] if row else 1

                new_xp = current_xp + earned_xp
                new_level = self._calc_level(new_xp)

                await conn.execute(
                    """
                    INSERT INTO vc_user_levels (guild_id, user_id, vc_xp, vc_level, last_active_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (guild_id, user_id) 
                    DO UPDATE SET 
                        vc_xp = EXCLUDED.vc_xp,
                        vc_level = EXCLUDED.vc_level,
                        last_active_at = EXCLUDED.last_active_at
                """,
                    guild_id,
                    user_id,
                    new_xp,
                    new_level,
                    datetime.now(timezone.utc),
                )

                # レベルアップ時のアナウンス
                if new_level > current_level and member and member.guild.system_channel:
                    rank_name, _ = self._get_vc_rank(new_level)
                    try:
                        await member.guild.system_channel.send(
                            f"🔊✨ {member.mention} が通話活動により **VC Lv.{new_level}** にアップ！\n"
                            f"称号: **{rank_name}**",
                            allowed_mentions=discord.AllowedMentions(users=False),
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"[VC XP加算エラー] guild:{guild_id} user:{user_id}: {e}")

    async def _flush_all_sessions(self, force_save: bool = False):
        """蓄積された滞在時間を集計してDBに書き込む"""
        if not self.active_vc_sessions:
            return

        now = datetime.now(timezone.utc)
        targets = list(self.active_vc_sessions.items())
        if force_save:
            self.active_vc_sessions.clear()

        for (guild_id, user_id), start_time in targets:
            minutes = (now - start_time).total_seconds() / 60.0
            if minutes < 1 and not force_save:
                continue

            # 1分につき 3〜6 XP を付与
            xp_to_add = int(minutes * random.randint(3, 6))
            if xp_to_add > 0:
                guild = self.bot.get_guild(guild_id)
                member = guild.get_member(user_id) if guild else None
                await self._add_vc_xp(guild_id, user_id, xp_to_add, member)

            if not force_save:
                self.active_vc_sessions[(guild_id, user_id)] = now

    @tasks.loop(minutes=3.0)
    async def save_vc_progress(self):
        """3分ごとに自動でVC通話XPを保存"""
        await self._flush_all_sessions(force_save=False)

    @save_vc_progress.before_loop
    async def before_save_vc_progress(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if member.bot:
            return

        g_id = member.guild.id
        u_id = member.id
        key = (g_id, u_id)
        now = datetime.now(timezone.utc)

        joined = before.channel is None and after.channel is not None
        left = before.channel is not None and after.channel is None
        muted = not (before.self_deaf or before.deaf) and (after.self_deaf or after.deaf)
        unmuted = (before.self_deaf or before.deaf) and not (after.self_deaf or after.deaf)

        # 参加 or ミュート解除
        if joined or unmuted:
            if after.channel and not (after.self_deaf or after.deaf):
                if len(after.channel.members) > 1:
                    self.active_vc_sessions[key] = now

        # 退出 or ミュート
        elif left or muted:
            if key in self.active_vc_sessions:
                start_time = self.active_vc_sessions.pop(key)
                mins = (now - start_time).total_seconds() / 60.0
                if mins >= 1:
                    earned = int(mins * random.randint(3, 6))
                    await self._add_vc_xp(g_id, u_id, earned, member)

        # チャンネル移動や人数変化による排他処理
        if after.channel:
            if len(after.channel.members) <= 1 and key in self.active_vc_sessions:
                start_time = self.active_vc_sessions.pop(key)
                mins = (now - start_time).total_seconds() / 60.0
                if mins >= 1:
                    earned = int(mins * random.randint(3, 6))
                    await self._add_vc_xp(g_id, u_id, earned, member)
            elif len(after.channel.members) > 1 and key not in self.active_vc_sessions and not (after.self_deaf or after.deaf):
                self.active_vc_sessions[key] = now

    # ==========================================
    # 📌 完全独立の専用スラッシュコマンド
    # ==========================================
    @app_commands.command(
        name="vclevel",
        description="あなたの現在のVC通話レベルとランクを確認します"
    )
    @app_commands.describe(target="確認したいユーザー（省略可能）")
    async def vclevel(self, interaction: discord.Interaction, target: discord.User | None = None):
        user = target or interaction.user
        if not interaction.guild or not self.db_pool:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT vc_xp, vc_level FROM vc_user_levels 
                WHERE guild_id = $1 AND user_id = $2
            """,
                interaction.guild.id,
                user.id,
            )

        xp = row["vc_xp"] if row else 0
        lvl = row["vc_level"] if row else 1
        rank_name, rank_color = self._get_vc_rank(lvl)
        next_xp = int(60 * (lvl**1.4))

        embed = discord.Embed(
            title=f"🎙️ {user.display_name} のVC通話ステータス",
            color=rank_color
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="VC称号", value=f"**{rank_name}**", inline=False)
        embed.add_field(name="VCレベル", value=f"**Lv. {lvl}**", inline=True)
        embed.add_field(name="VC XP", value=f"**{xp}** / {next_xp} XP", inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="vcrank",  # 競合しない任意の独立コマンド名
        description="このサーバーのVC通話時間ランキングTOP10を表示します"
    )
    async def vc_ranking(self, interaction: discord.Interaction):
        if not interaction.guild or not self.db_pool:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        await interaction.response.defer()

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, vc_xp, vc_level 
                FROM vc_user_levels 
                WHERE guild_id = $1 
                ORDER BY vc_xp DESC 
                LIMIT 10
            """,
                interaction.guild.id,
            )

        if not rows:
            await interaction.followup.send("まだこのサーバーにはVCレベルデータがありません。")
            return

        embed = discord.Embed(
            title=f"🏆 {interaction.guild.name} - VC通話ランキング",
            color=discord.Color.blue(),
        )

        lines = []
        for i, r in enumerate(rows, 1):
            m = interaction.guild.get_member(r["user_id"])
            name = m.display_name if m else f"ユーザー({r['user_id']})"
            r_name, _ = self._get_vc_rank(r["vc_level"])
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"**{i}.**"
            lines.append(f"{medal} **{name}** - Lv.{r['vc_level']} ({r_name}) | `{r['vc_xp']} XP`")

        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)
    @app_commands.command(
        name="sync_vc_beginner_roles",
        description="いずれかのVCランクロールを未所有のユーザーに初期ロール（Voice Novice）を一括付与します（管理者専用）",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_vc_beginner_roles(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        if not guild.me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "❌ ボットに「ロールの管理 (Manage Roles)」権限がありません。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        target_role_name = "🎙️🌱 Voice Novice (ビギナー)"
        beginner_role = discord.utils.get(guild.roles, name=target_role_name)

        if not beginner_role:
            try:
                beginner_role = await guild.create_role(
                    name=target_role_name,
                    color=discord.Color.from_rgb(50, 205, 50),
                    reason="VC初期ロールの自動生成",
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ ロール `{target_role_name}` が存在せず、作成権限もありません。"
                )
                return

        all_vc_role_names = {r_name for r_name, _ in VC_RANKS.values()}

        added_count = 0
        already_has_novice_count = 0
        has_higher_rank_count = 0

        members = guild.members
        if len(members) < guild.member_count:
            try:
                members = [m async for m in guild.fetch_members(limit=None)]
            except Exception:
                pass

        for member in members:
            if member.bot:
                continue

            user_vc_roles = [
                role.name for role in member.roles if role.name in all_vc_role_names
            ]

            if user_vc_roles:
                if target_role_name in user_vc_roles:
                    already_has_novice_count += 1
                else:
                    has_higher_rank_count += 1
                continue

            try:
                await member.add_roles(beginner_role, reason="VC初期ロールの一括付与")
                added_count += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"[{guild.name}] {member.display_name} へのVC初期ロール付与失敗: {e}")

        await interaction.followup.send(
            f"✅ **VC初期ロールの一括付与が完了しました！**\n"
            f"・新規付与: **{added_count} 人**\n"
            f"・既に Novice 所有済み: **{already_has_novice_count} 人**\n"
            f"・上位のVCランクを所有（除外）: **{has_higher_rank_count} 人**"
        )

    @sync_vc_beginner_roles.error
    async def sync_vc_beginner_roles_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ このコマンドを実行するには**管理者権限（Administrator）**が必要です。",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(IndependentVoiceLevelCog(bot))