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

# VC専用のランク定義
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
        self._roles_ensured: bool = False

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
            await self.ensure_vc_roles(guild)
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    if len(vc.members) == 1 or member.voice.self_deaf or member.voice.deaf:
                        continue
                    self.active_vc_sessions[(guild.id, member.id)] = now

        self.save_vc_progress.start()
        logger.info("[IndependentVoiceLevelCog] 初期化が完了しました。")

    async def cog_unload(self):
        """アンロード時に定期ループを停止し、残ったセッションをすべてセーブ"""
        self.save_vc_progress.cancel()
        await self._flush_all_sessions(force_save=True)
        if self.db_pool:
            await self.db_pool.close()

    def _calc_level(self, xp: int) -> int:
        return int((xp / 60) ** (1 / 1.4)) + 1

    def _get_vc_rank(self, level: int) -> tuple[str, discord.Color]:
        for req_lvl, (r_name, r_color) in VC_RANKS.items():
            if level >= req_lvl:
                return r_name, r_color
        return "🎙️🌱 Voice Novice (ビギナー)", discord.Color.from_rgb(50, 205, 50)

    async def ensure_vc_roles(self, guild: discord.Guild):
        """サーバー内に必要なVCランクロールが存在しなければ自動作成する"""
        if not guild.me.guild_permissions.manage_roles:
            return

        existing_role_names = {role.name for role in guild.roles}
        for _, (role_name, color) in VC_RANKS.items():
            if role_name not in existing_role_names:
                try:
                    await guild.create_role(
                        name=role_name,
                        color=color,
                        reason="VCレベルシステム用のロール自動生成",
                    )
                except Exception as e:
                    logger.error(f"[{guild.name}] VCロール作成エラー ({role_name}): {e}")

    async def update_user_vc_roles(self, member: discord.Member, new_level: int) -> bool:
        """レベルに応じてVCランクロールを自動付与・付け替え"""
        guild = member.guild
        if not guild.me.guild_permissions.manage_roles:
            return False

        target_rank_name, _ = self._get_vc_rank(new_level)
        all_rank_names = {info[0] for info in VC_RANKS.values()}
        target_role = discord.utils.get(guild.roles, name=target_rank_name)

        roles_to_remove = [
            role for role in member.roles
            if role.name in all_rank_names and role.name != target_rank_name
        ]

        is_rank_changed = False
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="VCレベルアップに伴う旧ランク削除")
            if target_role and target_role not in member.roles:
                await member.add_roles(target_role, reason="VCレベルアップに伴う新ランク付与")
                is_rank_changed = True
        except Exception as e:
            logger.error(f"[{guild.name}] {member.display_name} のVCロール更新失敗: {e}")

        return is_rank_changed

    async def get_notification_channel(
        self, guild: discord.Guild, origin_channel: Optional[discord.abc.GuildChannel], kind: str = "levelup"
    ) -> Optional[discord.TextChannel]:
        """SettingsCog と連携して通知チャンネルを取得する"""
        target_channel_id = None
        settings_cog = self.bot.get_cog("SettingsCog")

        if settings_cog:
            for method_name in ("get_vc_leveling_config", "get_leveling_config"):
                if hasattr(settings_cog, method_name):
                    try:
                        config_func = getattr(settings_cog, method_name)
                        config = config_func(guild.id)
                        if asyncio.iscoroutine(config):
                            config = await config

                        if config:
                            if isinstance(config, dict):
                                if kind == "levelup":
                                    target_channel_id = config.get("vc_levelup_channel_id") or config.get("log_vc_levelup_channel_id") or config.get("levelup_channel_id")
                                elif kind == "rankup":
                                    target_channel_id = config.get("vc_rankup_channel_id") or config.get("log_vc_rankup_channel_id") or config.get("rankup_channel_id") or config.get("vc_levelup_channel_id")
                            else:
                                if kind == "levelup":
                                    target_channel_id = getattr(config, "vc_levelup_channel_id", None) or getattr(config, "log_vc_levelup_channel_id", None) or getattr(config, "levelup_channel_id", None)
                                elif kind == "rankup":
                                    target_channel_id = getattr(config, "vc_rankup_channel_id", None) or getattr(config, "log_vc_rankup_channel_id", None) or getattr(config, "rankup_channel_id", None) or getattr(config, "vc_levelup_channel_id", None)
                        if target_channel_id:
                            break
                    except Exception as e:
                        logger.warning(f"SettingsCog からの設定取得に失敗しました: {e}")

        if target_channel_id:
            try:
                channel_id_int = int(target_channel_id)
                target_channel = guild.get_channel(channel_id_int)
                if target_channel is None:
                    try:
                        target_channel = await self.bot.fetch_channel(channel_id_int)
                    except Exception:
                        target_channel = None

                if (
                    target_channel
                    and isinstance(target_channel, discord.TextChannel)
                    and target_channel.permissions_for(guild.me).send_messages
                ):
                    return target_channel
            except ValueError:
                pass

        if kind == "rankup":
            return await self.get_notification_channel(guild, origin_channel, kind="levelup")

        return guild.text_channels[0] if guild.text_channels else (origin_channel if isinstance(origin_channel, discord.TextChannel) else None)

    async def _add_vc_xp(self, guild_id: int, user_id: int, earned_xp: int, member: Optional[discord.Member] = None):
        """データベースへのXP加算とレベルアップ・通知・ロール更新処理"""
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

                if new_level > current_level and member:
                    guild = member.guild
                    rank_name, _ = self._get_vc_rank(new_level)
                    prev_rank_name, _ = self._get_vc_rank(current_level)

                    # レベルアップ通知
                    text_channel = await self.get_notification_channel(guild, None, kind="levelup")
                    if text_channel:
                        try:
                            await text_channel.send(
                                f"🔊🎉 {member.mention} が通話活動により **VC Lv.{new_level}** にレベルアップ！\n"
                                f"現在のVCランク: **{rank_name}**",
                                allowed_mentions=discord.AllowedMentions(users=False),
                            )
                        except Exception:
                            pass

                    # ロール更新とランクアップ通知
                    is_rank_changed = await self.update_user_vc_roles(member, new_level)
                    if prev_rank_name != rank_name or is_rank_changed:
                        rank_channel = await self.get_notification_channel(guild, None, kind="rankup")
                        if rank_channel:
                            embed = discord.Embed(
                                title="👑 VC RANK UP!",
                                description=f"{member.mention} が新しいVCランク **【{rank_name}】** に昇格しました！",
                                color=discord.Color.purple(),
                            )
                            try:
                                await rank_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=False))
                            except Exception:
                                pass

        except Exception as e:
            logger.error(f"[VC XP加算エラー] guild:{guild_id} user:{user_id}: {e}")

    async def _flush_all_sessions(self, force_save: bool = False):
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

            xp_to_add = int(minutes * random.randint(3, 6))
            if xp_to_add > 0:
                guild = self.bot.get_guild(guild_id)
                member = guild.get_member(user_id) if guild else None
                await self._add_vc_xp(guild_id, user_id, xp_to_add, member)

            if not force_save:
                self.active_vc_sessions[(guild_id, user_id)] = now

    @tasks.loop(minutes=3.0)
    async def save_vc_progress(self):
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

        if joined or unmuted:
            if after.channel and not (after.self_deaf or after.deaf):
                if len(after.channel.members) > 1:
                    self.active_vc_sessions[key] = now

        elif left or muted:
            if key in self.active_vc_sessions:
                start_time = self.active_vc_sessions.pop(key)
                mins = (now - start_time).total_seconds() / 60.0
                if mins >= 1:
                    earned = int(mins * random.randint(3, 6))
                    await self._add_vc_xp(g_id, u_id, earned, member)

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
    # 📌 スラッシュコマンド群
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
        name="vcitems",
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