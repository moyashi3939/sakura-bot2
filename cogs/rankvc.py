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

# 冒険者ランクの設定（テキスト版と共通）
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


class VoiceLevelingCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_pool: Optional["asyncpg.Pool"] = None
        # {(guild_id, user_id): join_timestamp}
        self.voice_sessions: dict[tuple[int, int], datetime] = {}

    async def cog_load(self):
        """Cogロード時にDB接続 & 起動中のVCメンバーのセッション記録"""
        import asyncpg

        db_url = os.getenv("DATABASE_URL3")
        if not db_url:
            raise ValueError(
                "環境変数 'DATABASE_URL3' が設定されていません。"
            )

        self.db_pool = await asyncpg.create_pool(db_url)

        # ボット起動時にすでにVCに接続しているメンバーを検出して計測開始
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    # 自分自身のみ、またはミュート状態の場合は除外
                    if len(vc.members) == 1 or member.voice.self_deaf or member.voice.deaf:
                        continue
                    self.voice_sessions[(guild.id, member.id)] = now

        # 定期保存ループを開始
        self.flush_voice_xp.start()
        logger.info("[VoiceLevelingCog] データベース接続とVC監視ループが開始されました。")

    async def cog_unload(self):
        """Cogアンロード時にループ停止と未保存データの書き込み、DB切断"""
        self.flush_voice_xp.cancel()
        await self._save_all_sessions(is_unload=True)
        if self.db_pool:
            await self.db_pool.close()

    def calculate_level(self, xp: int) -> int:
        return int((xp / 100) ** (1 / 1.5)) + 1

    def get_rank_info(self, level: int) -> tuple[str, discord.Color]:
        for req_lvl, (rank_name, color) in ADVENTURER_RANKS.items():
            if level >= req_lvl:
                return rank_name, color
        return "🔰 Novice（駆け出し）", discord.Color.green()

    async def get_notification_channel(
        self, guild: discord.Guild, origin_channel: discord.abc.GuildChannel, kind: str = "levelup"
    ) -> discord.TextChannel:
        """SettingsCog から通知用チャンネルIDを取得する"""
        target_channel_id = None
        settings_cog = self.bot.get_cog("SettingsCog")

        if settings_cog and hasattr(settings_cog, "get_leveling_config"):
            try:
                config = settings_cog.get_leveling_config(guild.id)
                if asyncio.iscoroutine(config):
                    config = await config

                if config:
                    if isinstance(config, dict):
                        if kind == "levelup":
                            target_channel_id = config.get("log_levelup_channel_id") or config.get("levelup_channel_id")
                        elif kind == "rankup":
                            target_channel_id = config.get("log_rankup_channel_id") or config.get("rankup_channel_id") or config.get("log_levelup_channel_id")
                    else:
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

        # フォールバックとして、サーバー内の最初のテキストチャンネルを返す
        return guild.text_channels[0] if guild.text_channels else None

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
            role for role in member.roles
            if role.name in all_rank_names and role.name != target_rank_name
        ]

        is_rank_changed = False
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="VCレベルアップに伴う旧ランクの削除")
            if target_role and target_role not in member.roles:
                await member.add_roles(target_role, reason="VCレベルアップに伴う新ランクの付与")
                is_rank_changed = True
        except Exception as e:
            logger.error(f"[{guild.name}] {member.display_name} のロール更新失敗: {e}")

        return is_rank_changed

    async def _add_xp_to_db(self, guild_id: int, user_id: int, add_xp: int, member: Optional[discord.Member] = None):
        """データベースにXPを加算し、レベルアップ判定・通知を行う"""
        if not self.db_pool or add_xp <= 0:
            return

        try:
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

                new_xp = current_xp + add_xp
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

                if new_level > current_level and member:
                    guild = member.guild
                    rank_name, _ = self.get_rank_info(new_level)
                    prev_rank_name, _ = self.get_rank_info(current_level)

                    # 通知チャンネルの取得
                    text_channel = await self.get_notification_channel(guild, guild.system_channel, kind="levelup")
                    if text_channel:
                        try:
                            await text_channel.send(
                                f"🎙️🎉 {member.mention} がVC通話で **Lv.{new_level}** にレベルアップ！\n"
                                f"現在の発言ランク: **{rank_name}**",
                                allowed_mentions=discord.AllowedMentions(users=False),
                            )
                        except Exception:
                            pass

                    is_rank_changed = await self.update_user_roles(member, new_level)
                    if prev_rank_name != rank_name or is_rank_changed:
                        rank_channel = await self.get_notification_channel(guild, guild.system_channel, kind="rankup")
                        embed = discord.Embed(
                            title="👑 VOICE RANK UP!",
                            description=f"{member.mention} がVC活動により新しいランク **【{rank_name}】** に昇格しました！",
                            color=discord.Color.purple(),
                        )
                        try:
                            await rank_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=False))
                        except Exception:
                            pass

        except Exception as e:
            logger.error(f"[_add_xp_to_db エラー] guild:{guild_id} user:{user_id}: {e}")

    async def _save_all_sessions(self, is_unload: bool = False):
        """現在蓄積されているセッションの時間を計算してDBに反映し、セッションを更新する"""
        if not self.voice_sessions:
            return

        now = datetime.now(timezone.utc)
        sessions_to_process = list(self.voice_sessions.items())
        if is_unload:
            self.voice_sessions.clear()

        for (guild_id, user_id), start_time in sessions_to_process:
            duration_minutes = (now - start_time).total_seconds() / 60.0
            if duration_minutes < 1:
                if not is_unload:
                    continue  # 1分未満はスキップして次回に持ち越し
                else:
                    duration_minutes = 1  # アンロード時は端数を考慮

            # 1分あたり 2〜5 XP をランダム付与
            earned_xp = int(duration_minutes * random.randint(2, 5))
            if earned_xp > 0:
                guild = self.bot.get_guild(guild_id)
                member = guild.get_member(user_id) if guild else None
                await self._add_xp_to_db(guild_id, user_id, earned_xp, member)

            # セッション開始時刻を現在時刻にリセット（蓄積ループ用）
            if not is_unload:
                self.voice_sessions[(guild_id, user_id)] = now

    @tasks.loop(minutes=5.0)
    async def flush_voice_xp(self):
        """5分ごとにVC滞在分のXPをデータベースに定期保存する"""
        await self._save_all_sessions(is_unload=False)

    @flush_voice_xp.before_loop
    async def before_flush_voice_xp(self):
        await self.bot.wait_until_ready()

    # ==========================================
    # 🎧 VCイベントリスナー
    # ==========================================
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if member.bot:
            return

        guild_id = member.guild.id
        user_id = member.id
        key = (guild_id, user_id)
        now = datetime.now(timezone.utc)

        # 設定Cogによる有効確認
        settings_cog = self.bot.get_cog("SettingsCog")
        if settings_cog and hasattr(settings_cog, "get_leveling_config"):
            try:
                config = await settings_cog.get_leveling_config(guild_id)
                if config and not config.get("enabled", True):
                    return
            except Exception:
                pass

        # 1. VCに参加した、またはミュートを解除した（測定開始・再開）
        joined_vc = before.channel is None and after.channel is not None
        unmuted = (before.self_deaf or before.deaf) and not (after.self_deaf or after.deaf)
        
        # 2. VCから退出した、またはミュートにした（測定終了・精算）
        left_vc = before.channel is not None and after.channel is None
        muffled = not (before.self_deaf or before.deaf) and (after.self_deaf or after.deaf)

        # 参加時判定
        if joined_vc or unmuted:
            if after.channel and not (after.self_deaf or after.deaf):
                # 部屋に自分以外のメンバーが他にいるかチェック（一人ぼっち対策）
                if len(after.channel.members) > 1:
                    self.voice_sessions[key] = now

        # 退出・ミュート時判定
        elif left_vc or muffled:
            if key in self.voice_sessions:
                start_time = self.voice_sessions.pop(key)
                duration_minutes = (now - start_time).total_seconds() / 60.0
                if duration_minutes >= 1:
                    earned_xp = int(duration_minutes * random.randint(2, 5))
                    await self._add_xp_to_db(guild_id, user_id, earned_xp, member)

        # 移動した場合や人数変化の考慮
        if after.channel:
            # 部屋の人数が自分含めて1人になった場合、計測を一旦精算して停止
            if len(after.channel.members) <= 1 and key in self.voice_sessions:
                start_time = self.voice_sessions.pop(key)
                duration_minutes = (now - start_time).total_seconds() / 60.0
                if duration_minutes >= 1:
                    earned_xp = int(duration_minutes * random.randint(2, 5))
                    await self._add_xp_to_db(guild_id, user_id, earned_xp, member)
            # 部屋の人数が2人以上になり、かつまだセッションがなく、ミュートでない場合
            elif len(after.channel.members) > 1 and key not in self.voice_sessions and not (after.self_deaf or after.deaf):
                self.voice_sessions[key] = now


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceLevelingCog(bot))