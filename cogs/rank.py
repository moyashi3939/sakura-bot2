import asyncio
from datetime import datetime, timezone
import logging
import os
import random
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool
from utils.perms import admin_or_owner

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# 冒険者ランクの設定 (必要レベル : (ロール名, ロールカラー))
ADVENTURER_RANKS = {
    777: ("🪽 God (神)", discord.Color.from_rgb(255, 0, 0)),  # 赤
    600: ("👑 Legend（伝説の勇者）", discord.Color.from_rgb(255, 215, 0)),  # 黄金
    400: ("🛡️ Adamantite（金剛級）", discord.Color.from_rgb(112, 128, 144)),  # アダマンタイト
    200: ("🔮 Mythril（神銀級）", discord.Color.from_rgb(138, 43, 226)),  # ミスリル
    100: ("⚜️ Platinum（白金級）", discord.Color.from_rgb(229, 228, 226)),  # プラチナ
    50: ("🥇 Gold（黄金級）", discord.Color.gold()),  # ゴールド
    25: ("⚔️ Silver（白銀級）", discord.Color.light_grey()),  # シルバー
    5: ("🗡️ Bronze（青銅級）", discord.Color.dark_orange()),  # ブロンズ
    1: ("🔰 Novice（駆け出し）", discord.Color.green()),  # グリーン
}


class LevelingCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_pool: Optional["asyncpg.Pool"] = None
        self._roles_ensured: bool = False  # on_ready の重複実行防止フラグ

    async def cog_load(self):
        """Cogロード時にDB接続 & テーブル自動作成"""
        self.db_pool = await get_pool(self.bot)  # 共有プール

        async with self.db_pool.acquire() as conn:
            # ユーザーレベル管理テーブル
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_levels (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    xp INT DEFAULT 0,
                    level INT DEFAULT 1,
                    last_message_at TIMESTAMP WITH TIME ZONE,
                    PRIMARY KEY (guild_id, user_id)
                );
            """
            )
        logger.info("[LevelingCog] データベース接続とテーブル確認が完了しました。")

    def calculate_level(self, xp: int) -> int:
        """累積XPからレベルを計算"""
        return int((xp / 100) ** (1 / 1.5)) + 1

    def calculate_required_xp(self, level: int) -> int:
        """レベル到達に必要な累積XPを計算"""
        if level <= 1:
            return 0
        return int(100 * ((level - 1) ** 1.5))

    def get_rank_info(self, level: int) -> tuple[str, discord.Color]:
        """レベルに応じた (ロール名, ロールカラー) を返す"""
        for req_lvl, (rank_name, color) in ADVENTURER_RANKS.items():
            if level >= req_lvl:
                return rank_name, color
        return "🔰 Novice（駆け出し）", discord.Color.green()

    # ==========================================
    # 📢 通知チャンネル取得メソッド (SettingsCog 連携: キー修正済み)
    # ==========================================
    async def get_notification_channel(
        self, guild: discord.Guild, origin_channel: discord.TextChannel, kind: str = "levelup"
    ) -> discord.TextChannel:
        """SettingsCog から通知用チャンネルIDを取得する"""
        target_channel_id = None
        settings_cog = self.bot.get_cog("SettingsCog")

        if settings_cog and hasattr(settings_cog, "get_leveling_config"):
            try:
                # get_leveling_config がコルーチン（async def）か通常関数かに応じて await
                config = settings_cog.get_leveling_config(guild.id)
                if asyncio.iscoroutine(config):
                    config = await config

                if config:
                    # 辞書型なのかオブジェクト（属性アクセス）なのかで分岐
                    if isinstance(config, dict):
                        if kind == "levelup":
                            target_channel_id = config.get("log_levelup_channel_id") or config.get("levelup_channel_id")
                        elif kind == "rankup":
                            target_channel_id = config.get("log_rankup_channel_id") or config.get("rankup_channel_id") or config.get("log_levelup_channel_id")
                    else:
                        # オブジェクト形式の場合
                        if kind == "levelup":
                            target_channel_id = getattr(config, "log_levelup_channel_id", None) or getattr(config, "levelup_channel_id", None)
                        elif kind == "rankup":
                            target_channel_id = getattr(config, "log_rankup_channel_id", None) or getattr(config, "rankup_channel_id", None) or getattr(config, "log_levelup_channel_id", None)
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
                logger.error(f"取得されたチャンネルID ({target_channel_id}) を int に変換できませんでした。")

        if kind == "rankup":
            return await self.get_notification_channel(guild, origin_channel, kind="levelup")

        return origin_channel
    async def ensure_roles(
        self, guild: discord.Guild
    ) -> tuple[list[str], list[str]]:
        """サーバー内に必要な発言ランクロールが存在しなければ自動作成する"""
        if not guild.me.guild_permissions.manage_roles:
            return [], []

        existing_role_names = {role.name for role in guild.roles}
        created_roles = []
        already_existing_roles = []

        for req_lvl, (role_name, color) in ADVENTURER_RANKS.items():
            if role_name not in existing_role_names:
                try:
                    await guild.create_role(
                        name=role_name,
                        color=color,
                        reason="発言レベルシステム用のロール自動生成",
                    )
                    created_roles.append(role_name)
                except discord.Forbidden:
                    logger.warning(f"[{guild.name}] ロール作成権限が不足しています: {role_name}")
                except Exception as e:
                    logger.error(f"[{guild.name}] ロール作成エラー ({role_name}): {e}")
            else:
                already_existing_roles.append(role_name)

        return created_roles, already_existing_roles

    async def update_user_roles(
        self, member: discord.Member, new_level: int
    ) -> bool:
        """レベルに応じて発言ランクロールを自動付与・付け替え"""
        guild = member.guild
        if not guild.me.guild_permissions.manage_roles:
            return False

        target_rank_name, _ = self.get_rank_info(new_level)
        all_rank_names = {info[0] for info in ADVENTURER_RANKS.values()}

        target_role = discord.utils.get(guild.roles, name=target_rank_name)

        roles_to_remove = [
            role
            for role in member.roles
            if role.name in all_rank_names and role.name != target_rank_name
        ]

        is_rank_changed = False
        try:
            if roles_to_remove:
                await member.remove_roles(
                    *roles_to_remove, reason="レベルアップに伴う旧ランクの削除"
                )

            if target_role and target_role not in member.roles:
                await member.add_roles(
                    target_role, reason="レベルアップに伴う新ランクの付与"
                )
                is_rank_changed = True
        except discord.Forbidden:
            logger.warning(f"[{guild.name}] ロール変更権限が不足しています")
        except Exception as e:
            logger.error(f"[{guild.name}] {member.display_name} のロール更新失敗: {e}")

        return is_rank_changed

    # ==========================================
    # 🎧 イベントリスナー
    # ==========================================
    @commands.Cog.listener()
    async def on_ready(self):
        if not self._roles_ensured:
            for guild in self.bot.guilds:
                await self.ensure_roles(guild)
            self._roles_ensured = True
            logger.info("[LevelingCog] on_ready: ロールの確認・自動生成が完了しました。")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.ensure_roles(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not self.db_pool:
            return

        # SettingsCog から有効フラグを確認
        settings_cog = self.bot.get_cog("SettingsCog")
        if settings_cog and hasattr(settings_cog, "get_leveling_config"):
            try:
                config = await settings_cog.get_leveling_config(message.guild.id)
                if config and not config.get("enabled", True):
                    return  # レベリングが無効化されている場合はスキップ
            except Exception:
                pass

        guild_id = message.guild.id
        user_id = message.author.id
        now = datetime.now(timezone.utc)

        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT xp, level, last_message_at 
                    FROM user_levels 
                    WHERE guild_id = $1 AND user_id = $2
                """,
                    guild_id,
                    user_id,
                )

                if row and row["last_message_at"]:
                    delta = (now - row["last_message_at"]).total_seconds()
                    if delta < 30:
                        return

                current_xp = row["xp"] if row else 0
                current_level = row["level"] if row else 1

                added_xp = random.randint(10, 30)
                new_xp = current_xp + added_xp
                new_level = self.calculate_level(new_xp)

                await conn.execute(
                    """
                    INSERT INTO user_levels (guild_id, user_id, xp, level, last_message_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (guild_id, user_id) 
                    DO UPDATE SET 
                        xp = EXCLUDED.xp,
                        level = EXCLUDED.level,
                        last_message_at = EXCLUDED.last_message_at
                """,
                    guild_id,
                    user_id,
                    new_xp,
                    new_level,
                    now,
                )

                if new_level > current_level:
                    rank_name, _ = self.get_rank_info(new_level)

                    if isinstance(message.channel, discord.TextChannel):
                        level_channel = await self.get_notification_channel(
                            message.guild, message.channel, kind="levelup"
                        )

                        try:
                            await level_channel.send(
                                f"🎉 {message.author.mention} が **Lv.{new_level}** にレベルアップ！\n"
                                f"現在の発言ランク: **{rank_name}**",
                                allowed_mentions=discord.AllowedMentions(users=False),
                            )
                        except discord.Forbidden:
                            logger.warning(f"チャンネル {level_channel.name} への送信権限がありません。")

                    if isinstance(message.author, discord.Member):
                        prev_rank_name, _ = self.get_rank_info(current_level)
                        is_rank_changed = await self.update_user_roles(
                            message.author, new_level
                        )

                        if (prev_rank_name != rank_name or is_rank_changed) and isinstance(
                            message.channel, discord.TextChannel
                        ):
                            rank_channel = await self.get_notification_channel(
                                message.guild, message.channel, kind="rankup"
                            )
                            embed = discord.Embed(
                                title="👑 RANK UP!",
                                description=f"{message.author.mention} が新しいランク **【{rank_name}】** に昇格しました！",
                                color=discord.Color.purple(),
                            )
                            try:
                                await rank_channel.send(
                                    embed=embed,
                                    allowed_mentions=discord.AllowedMentions(users=False),
                                )
                            except discord.Forbidden:
                                pass

        except Exception as e:
            logger.error(f"on_message の処理中にエラーが発生しました: {e}", exc_info=True)

    # ==========================================
    # ⚔️ スラッシュコマンド群
    # ==========================================

    # --- /level コマンド ---
    @app_commands.command(
        name="level", description="自分または指定ユーザーのレベルと発言ランクを確認します"
    )
    @app_commands.describe(target_user="確認したいユーザー（省略した場合は自分になります）")
    async def level(
        self,
        interaction: discord.Interaction,
        target_user: discord.User | None = None,
    ):
        user = target_user or interaction.user
        guild_id = interaction.guild_id

        if not interaction.guild or not self.db_pool or not guild_id:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT xp, level FROM user_levels 
                WHERE guild_id = $1 AND user_id = $2
            """,
                guild_id,
                user.id,
            )

        xp = row["xp"] if row else 0
        level = row["level"] if row else 1

        next_level_xp = int(100 * (level**1.5))
        rank_name, rank_color = self.get_rank_info(level)

        embed = discord.Embed(
            title=f"⚔️ {user.display_name} の発言ステータス", color=rank_color
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="発言ランク", value=f"**{rank_name}**", inline=False)
        embed.add_field(name="レベル", value=f"**Lv. {level}**", inline=True)
        embed.add_field(
            name="XP", value=f"**{xp}** / {next_level_xp} XP", inline=True
        )

        await interaction.response.send_message(embed=embed)

    # --- /rank コマンド ---
    @app_commands.command(
        name="rank",
        description="このサーバーの発言数レベルランキングTOP 10を表示します",
    )
    async def rank(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild or not self.db_pool:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        await interaction.response.defer()

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, xp, level 
                FROM user_levels 
                WHERE guild_id = $1 
                ORDER BY xp DESC 
                LIMIT 10
            """,
                guild.id,
            )

        if not rows:
            await interaction.followup.send(
                "まだこのサーバーにはレベルデータが存在しません。"
            )
            return

        embed = discord.Embed(
            title=f"🏆 {guild.name} の発言数レベルランキング",
            color=discord.Color.gold(),
        )

        rank_list = []
        for index, row in enumerate(rows, start=1):
            user_id = row["user_id"]
            xp = row["xp"]
            level = row["level"]

            member = guild.get_member(user_id)
            user_name = member.display_name if member else f"ユーザー({user_id})"
            rank_name, _ = self.get_rank_info(level)

            medal = (
                "🥇"
                if index == 1
                else "🥈" if index == 2 else "🥉" if index == 3 else f"**{index}.**"
            )
            rank_list.append(
                f"{medal} **{user_name}** - Lv.{level} ({rank_name}) | `{xp} XP`"
            )

        embed.description = "\n".join(rank_list)
        await interaction.followup.send(embed=embed)

    # --- /role コマンド (管理者限定) ---
    @app_commands.command(
        name="role",
        description="発言システム用ロールの確認および未作成ロールを追加します（管理者専用）",
    )
    @app_commands.guild_only()
    @admin_or_owner()
    async def role(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        created_roles, _ = await self.ensure_roles(guild)

        if not created_roles:
            await interaction.response.send_message(
                "✅ 発言システムに必要なロールはすでにすべて存在しています！",
                ephemeral=True,
            )
            return

        created_list_str = "\n".join([f"・{r}" for r in created_roles])
        embed = discord.Embed(
            title="🔨 発言ランクロールの自動生成完了",
            description=f"不足していた以下のロールを追加しました：\n\n{created_list_str}",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- サーバー全員にビギナーロールを一括付与するコマンド (管理者限定) ---
    @app_commands.command(
        name="sync_beginner_roles",
        description="いずれかのランクロールを未所有のユーザーに初期ロール（Novice）を一括付与します（管理者専用）",
    )
    @app_commands.guild_only()
    @admin_or_owner()
    async def sync_beginner_roles(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        await self.ensure_roles(guild)

        target_role_name = "🔰 Novice（駆け出し）"
        beginner_role = discord.utils.get(guild.roles, name=target_role_name)

        if not beginner_role:
            await interaction.followup.send(
                f"❌ ロール `{target_role_name}` が見つかりませんでした。"
            )
            return

        all_rank_role_names = {info[0] for info in ADVENTURER_RANKS.values()}

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

            user_rank_roles = [
                role.name for role in member.roles if role.name in all_rank_role_names
            ]

            if user_rank_roles:
                if target_role_name in user_rank_roles:
                    already_has_novice_count += 1
                else:
                    has_higher_rank_count += 1
                continue

            try:
                await member.add_roles(beginner_role, reason="一括初期ロール付与")
                added_count += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(
                    f"[{guild.name}] {member.display_name} へのロール付与失敗: {e}"
                )

        await interaction.followup.send(
            f"✅ **一括付与が完了しました！**\n"
            f"・新規付与: **{added_count} 人**\n"
            f"・既に Novice 所有済み: **{already_has_novice_count} 人**\n"
            f"・上位ランク所有（除外）: **{has_higher_rank_count} 人**"
        )

    # --- /add_exp コマンド (ボットオーナー限定) ---
    @app_commands.command(
        name="add_exp",
        description="指定したユーザーにXPを追加し、必要に応じてレベルアップやロール更新を行います（ボットオーナー専用）",
    )
    @app_commands.describe(
        target_user="XPを追加するユーザー",
        amount="追加するXP量（マイナスを指定して減らすことも可能）"
    )
    async def add_exp(
        self,
        interaction: discord.Interaction,
        target_user: discord.Member,
        amount: int,
    ):
        # ボットオーナー判定（OWNER_IDS も含む）
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ このコマンドは**ボットオーナー**のみ実行できます。", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild or not self.db_pool:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        if target_user.bot:
            await interaction.response.send_message(
                "❌ ボットにXPを付与することはできません。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = guild.id
        user_id = target_user.id

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT xp, level FROM user_levels 
                WHERE guild_id = $1 AND user_id = $2
            """,
                guild_id,
                user_id,
            )

            current_xp = row["xp"] if row else 0
            current_level = row["level"] if row else 1

            new_xp = max(0, current_xp + amount)
            new_level = self.calculate_level(new_xp)

            await conn.execute(
                """
                INSERT INTO user_levels (guild_id, user_id, xp, level)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, user_id) 
                DO UPDATE SET 
                    xp = EXCLUDED.xp,
                    level = EXCLUDED.level
            """,
                guild_id,
                user_id,
                new_xp,
                new_level,
            )

        if new_level != current_level and isinstance(target_user, discord.Member):
            await self.update_user_roles(target_user, new_level)

        await interaction.followup.send(
            f"✅ **{target_user.display_name}** のXPを `{amount:+d}` しました。\n"
            f"・合計XP: `{current_xp} ➔ {new_xp} XP`\n"
            f"・レベル: `Lv.{current_level} ➔ Lv.{new_level}`"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))