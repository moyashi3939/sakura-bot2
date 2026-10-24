import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.db import get_pool

logger = logging.getLogger(__name__)

# リマインダーの時刻はこのタイムゾーンで解釈する（.env の TZ_NAME で変更可）
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tokyo"))

DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6
}
DAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]

MAX_REMINDERS_PER_GUILD = 100


def _now() -> datetime:
    """タイムゾーン情報を外した現在時刻（DBの TIMESTAMP と比較するため）"""
    return datetime.now(TZ).replace(tzinfo=None)


class Reminder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None

    async def cog_load(self):
        self.db_pool = await get_pool(self.bot)  # 共有プール
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT,
                    channel_id BIGINT NOT NULL,
                    mention TEXT NOT NULL,
                    message TEXT NOT NULL,
                    time TIMESTAMP,
                    daily_time TIME,
                    repeat_days INTEGER[],
                    last_fired DATE
                );
            """)
            await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS guild_id BIGINT;")
            await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS daily_time TIME;")
            await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat_days INTEGER[];")
            await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS last_fired DATE;")
            # 登録者（削除権限の判定用）。既存の行は NULL のまま（管理者/オーナーのみ削除可）
            await conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS creator_id BIGINT;")
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()  # 共有プールは閉じない

    async def get_target_channel_id(self, guild_id, original_channel_id):
        # SettingsCogで設定されたIDがあれば優先、なければ登録時のID
        if guild_id:
            settings_cog = self.bot.get_cog("SettingsCog")
            if settings_cog and hasattr(settings_cog, "get_reminder_channel_id"):
                guild_reminder_ch = await settings_cog.get_reminder_channel_id(guild_id)
                if guild_reminder_ch:
                    return guild_reminder_ch
        return original_channel_id

    async def _send(self, row, suffix: str = ""):
        target_id = await self.get_target_channel_id(row["guild_id"], row["channel_id"])
        channel = self.bot.get_channel(target_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(target_id)
            except discord.HTTPException:
                return
        # 登録時に許可されたメンションだけを通す（@everyone は登録時に管理者のみ許可）
        allowed = discord.AllowedMentions(everyone=True, users=True, roles=True)
        try:
            await channel.send(f"{row['mention']} {row['message']}{suffix}", allowed_mentions=allowed)
        except discord.HTTPException as e:
            logger.warning("リマインダー送信失敗 (id=%s): %s", row["id"], e)

    @tasks.loop(seconds=15)
    async def check_reminders(self):
        now = _now()
        today = now.date()
        weekday = now.weekday()

        async with self.db_pool.acquire() as conn:
            # 1回限り: 時刻を過ぎていれば送信（Bot停止中に過ぎた分も起動後に送る）
            rows = await conn.fetch(
                "SELECT id, guild_id, channel_id, mention, message, time FROM reminders "
                "WHERE time IS NOT NULL AND time <= $1", now
            )
            for row in rows:
                late = now - row["time"]
                suffix = f"\n（⚠️ {int(late.total_seconds() // 60)}分遅れで送信）" if late > timedelta(minutes=2) else ""
                await self._send(row, suffix)
                await conn.execute("DELETE FROM reminders WHERE id = $1", row["id"])

            # 毎週: 当日の指定時刻を過ぎていて、まだ今日送っていなければ送信
            rows_weekly = await conn.fetch(
                "SELECT id, guild_id, channel_id, mention, message, daily_time, repeat_days, last_fired "
                "FROM reminders WHERE repeat_days IS NOT NULL"
            )
            for row in rows_weekly:
                if not row["daily_time"] or weekday not in row["repeat_days"]:
                    continue
                if row["last_fired"] == today:
                    continue
                due_dt = datetime.combine(today, row["daily_time"]).replace(second=0, microsecond=0)
                # 5分以内の遅れまで救済（長時間停止後の大量送信を避ける）
                if due_dt <= now <= due_dt + timedelta(minutes=5):
                    await self._send(row)
                    await conn.execute("UPDATE reminders SET last_fired = $1 WHERE id = $2", today, row["id"])

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    @check_reminders.error
    async def check_reminders_error(self, error):
        logger.error("リマインダーループでエラー: %r", error, exc_info=(type(error), error, error.__traceback__))

    # ------------------------------------------------------------
    # 共通: メンション文字列の検証
    # ------------------------------------------------------------
    async def _validate_mention(self, interaction: discord.Interaction, mention: str) -> str | None:
        """問題があればエラーメッセージを返す。@everyone / @here は管理者・オーナーのみ。"""
        if len(mention) > 200:
            return "❌ メンションが長すぎます（200文字以内）。"
        if "@everyone" in mention or "@here" in mention:
            is_admin = (
                interaction.guild is not None and interaction.permissions.mention_everyone
            ) or await self.bot.is_owner(interaction.user)
            if not is_admin:
                return "❌ `@everyone` / `@here` を指定できるのは「全員にメンション」権限を持つ人（またはBotオーナー）のみです。"
        return None

    async def _count_ok(self, interaction: discord.Interaction) -> bool:
        async with self.db_pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM reminders WHERE guild_id = $1", interaction.guild_id)
        if n >= MAX_REMINDERS_PER_GUILD and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                f"❌ このサーバーのリマインダーは最大{MAX_REMINDERS_PER_GUILD}件までです。不要なものを `/remind_del` で削除してください。",
                ephemeral=True,
            )
            return False
        return True

    @app_commands.command(name="remind", description="日時を指定して1回限りのリマインダーを設定します")
    @app_commands.describe(
        datetime_str="日時 (例: 2026-12-31 18:30)  ※日本時間",
        mention="メンション先 (例: <@ユーザーID> / <@&ロールID> / 任意の文字)",
        message="送信する内容",
    )
    @app_commands.guild_only()
    async def set_reminder(self, interaction: discord.Interaction, datetime_str: str, mention: str, message: str):
        try:
            dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
        except ValueError:
            await interaction.response.send_message("❌ 日付と時刻は `YYYY-MM-DD HH:MM` 形式で指定してください。", ephemeral=True)
            return
        if dt <= _now():
            await interaction.response.send_message("❌ 過去の日時は指定できません。", ephemeral=True)
            return
        if len(message) > 1500:
            await interaction.response.send_message("❌ メッセージが長すぎます（1500文字以内）。", ephemeral=True)
            return
        if (err := await self._validate_mention(interaction, mention)):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not await self._count_ok(interaction):
            return

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO reminders (guild_id, channel_id, mention, message, time, creator_id) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                interaction.guild_id, interaction.channel_id, mention, message, dt, interaction.user.id,
            )
        await interaction.response.send_message(
            f"✅ リマインダーを設定しました！\n日時: `{datetime_str}`\n対象: {mention}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="remind_weekly", description="曜日を指定して毎週のリマインダーを設定します")
    @app_commands.describe(
        time_str="時刻 (例: 21:00)  ※日本時間",
        days="曜日をカンマ区切りで (例: 月,水,金 / mon,wed)",
        mention="メンション先",
        message="送信する内容",
    )
    @app_commands.guild_only()
    async def set_weekly_reminder(self, interaction: discord.Interaction, time_str: str, days: str, mention: str, message: str):
        try:
            t = datetime.strptime(time_str, "%H:%M").time()
        except ValueError:
            await interaction.response.send_message("❌ 時刻は `HH:MM` 形式で指定してください。", ephemeral=True)
            return

        day_list = sorted({DAY_MAP[d.strip().lower()] for d in days.split(",") if d.strip().lower() in DAY_MAP})
        if not day_list:
            await interaction.response.send_message(
                "❌ 曜日を認識できませんでした。`月,水,金` や `mon,wed` のように指定してください。", ephemeral=True
            )
            return
        if len(message) > 1500:
            await interaction.response.send_message("❌ メッセージが長すぎます（1500文字以内）。", ephemeral=True)
            return
        if (err := await self._validate_mention(interaction, mention)):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not await self._count_ok(interaction):
            return

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO reminders (guild_id, channel_id, mention, message, daily_time, repeat_days, creator_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                interaction.guild_id, interaction.channel_id, mention, message, t, day_list, interaction.user.id,
            )
        day_text = "・".join(DAY_LABELS[d] for d in day_list)
        await interaction.response.send_message(
            f"🔄 毎週のリマインダーを設定しました！\n時刻: `{time_str}` / 曜日: {day_text}\n対象: {mention}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="remind_list", description="登録されているリマインダーの一覧を表示します")
    @app_commands.guild_only()
    async def list_reminders(self, interaction: discord.Interaction):
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM reminders WHERE guild_id = $1 ORDER BY time ASC NULLS LAST, daily_time ASC LIMIT 25",
                interaction.guild_id,
            )

        if not rows:
            await interaction.response.send_message("📭 登録されているリマインダーはありません。", ephemeral=True)
            return

        embed = discord.Embed(title="⏰ 登録済みリマインダー一覧", color=discord.Color.green())
        for row in rows:
            if row["time"]:
                info = f"📅 日時: `{row['time'].strftime('%Y-%m-%d %H:%M')}`"
            else:
                day_names = [DAY_LABELS[d] for d in row["repeat_days"] or []]
                info = f"🔄 時刻: `{row['daily_time'].strftime('%H:%M')}` / 曜日: `{', '.join(day_names)}`"
            owner_text = f"\n✍️ 登録者: <@{row['creator_id']}>" if row["creator_id"] else ""
            embed.add_field(
                name=f"ID: {row['id']} | {row['message'][:80]}",
                value=f"{info}\n👤 対象: {row['mention']}{owner_text}",
                inline=False,
            )
        if len(rows) == 25:
            embed.set_footer(text="※先頭25件のみ表示しています")
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="remind_del", description="指定したIDのリマインダーを削除します")
    @app_commands.guild_only()
    async def delete_reminder(self, interaction: discord.Interaction, reminder_id: int):
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, creator_id FROM reminders WHERE id = $1 AND guild_id = $2",
                reminder_id, interaction.guild_id,
            )
            if row is None:
                await interaction.response.send_message("❌ 指定したIDのリマインダーが見つかりません。", ephemeral=True)
                return

            # 削除できるのは「登録者本人」「サーバー管理権限を持つ人」「ボットオーナー」
            can_delete = (
                row["creator_id"] == interaction.user.id
                or interaction.permissions.manage_guild
                or await self.bot.is_owner(interaction.user)
            )
            if not can_delete:
                await interaction.response.send_message("❌ このリマインダーを削除できるのは、登録者かサーバー管理者のみです。", ephemeral=True)
                return

            await conn.execute("DELETE FROM reminders WHERE id = $1", reminder_id)
        await interaction.response.send_message(f"🗑️ ID `{reminder_id}` を削除しました。", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Reminder(bot))
