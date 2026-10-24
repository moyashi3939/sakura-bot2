import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
import time
import sys
import logging
from datetime import datetime, timedelta, timezone
from discord.ext import tasks

from utils.db import get_pool
from utils.perms import prefix_admin_or_owner, prefix_owner_only

# --- ルーレット用のView ---
class MemberRouletteView(discord.ui.View):
    def __init__(self, target_members):
        super().__init__(timeout=60)
        self.target_members = target_members

    @discord.ui.button(label="もう一度引く 🔁", style=discord.ButtonStyle.green)
    async def retry_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🎰 **サーバーメンバー全員から選出中... ドラムロールスタート！**", embed=None)
        await asyncio.sleep(2.0)
        chosen_member = random.choice(self.target_members)
        embed = discord.Embed(
            title="🎯 ルーレット結果",
            description=f"選ばれたのは... 🎉 {chosen_member.mention} 🎉 です！",
            color=discord.Color.red()
        )
        await interaction.edit_original_response(content=f"🎯 {chosen_member.mention} ロックオン！", embed=embed, view=self)


logger = logging.getLogger(__name__)

MAX_TIMERS_PER_USER = 5


# --- メインのCogクラス ---
class CommandsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pool = None

    async def cog_load(self):
        # タイマーはDBに保存するので、Botを再起動しても消えない
        self.pool = await get_pool(self.bot)
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS timers (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT,
                    channel_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    fire_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    message TEXT
                );
            """)
        self.check_timers.start()

    def cog_unload(self):
        self.check_timers.cancel()

    @tasks.loop(seconds=10)
    async def check_timers(self):
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, channel_id, user_id, message, fire_at FROM timers WHERE fire_at <= $1", now
            )
            for row in rows:
                # 先に削除して二重送信を防ぐ
                await conn.execute("DELETE FROM timers WHERE id = $1", row["id"])
                channel = self.bot.get_channel(row["channel_id"])
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(row["channel_id"])
                    except discord.HTTPException:
                        continue
                late = now - row["fire_at"]
                note = f"\n（⚠️ Bot停止などで{int(late.total_seconds() // 60)}分遅れました）" if late > timedelta(minutes=2) else ""
                text = row["message"] or "時間になりました！"
                try:
                    await channel.send(
                        f"⏰ <@{row['user_id']}> さん、タイマーの時間です！\n{text}{note}",
                        allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row["user_id"])]),
                    )
                except discord.HTTPException as e:
                    logger.warning("タイマー通知の送信に失敗 (id=%s): %s", row["id"], e)

    @check_timers.before_loop
    async def before_check_timers(self):
        await self.bot.wait_until_ready()

    @check_timers.error
    async def check_timers_error(self, error):
        logger.error("タイマーループでエラー: %r", error, exc_info=(type(error), error, error.__traceback__))

    # --- スラッシュコマンド (一般向け) ---

    @app_commands.command(name="omikuji", description="今日のおみくじを引きます")
    async def omikuji(self, interaction: discord.Interaction):
        fortunes = ["大吉 🌟", "吉 🎯", "中吉 😊", "小吉 👍", "末吉 🌿", "凶 👻", "半吉", "元凶", "大凶", "半凶", "大狂", "狂","勉強"]
        result = random.choice(fortunes)
        await interaction.response.send_message(f"{interaction.user.mention} さんの今日の運勢は... **{result}** です！")

    @app_commands.command(name="timer", description="指定した分数後にメンションで通知します（最大180分・再起動しても消えません）")
    @app_commands.describe(minutes="タイマーの分数 (1〜180分)", message="通知に添えるメッセージ（省略可）")
    @app_commands.guild_only()
    async def timer(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 180], message: app_commands.Range[str, 0, 200] = ""):
        async with self.pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM timers WHERE user_id = $1", interaction.user.id)
            if count >= MAX_TIMERS_PER_USER:
                await interaction.response.send_message(
                    f"❌ 同時に設定できるタイマーは{MAX_TIMERS_PER_USER}個までです。", ephemeral=True
                )
                return
            fire_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            await conn.execute(
                "INSERT INTO timers (guild_id, channel_id, user_id, fire_at, message) VALUES ($1, $2, $3, $4, $5)",
                interaction.guild_id, interaction.channel_id, interaction.user.id, fire_at, message or None,
            )
        await interaction.response.send_message(
            f"🔔 {minutes}分間のタイマーを開始しました。{discord.utils.format_dt(fire_at, 'T')} にお知らせします！"
        )

    @app_commands.command(name="roulette", description="サーバー内の全メンバーからランダムに1人選んでメンションします")
    async def roulette(self, interaction: discord.Interaction):
        guild = interaction.guild
        candidates = [m for m in guild.members if not m.bot]
        if not candidates:
            await interaction.response.send_message("❌ 抽選対象となるメンバーが見つかりませんでした。", ephemeral=True)
            return
        await interaction.response.send_message(f"🎰 **全サーバーメンバーから選出中... ドラムロールスタート！**")
        await asyncio.sleep(2.0)
        chosen_member = random.choice(candidates)
        embed = discord.Embed(title="🎯 ルーレット結果", description=f"対象: 全サーバーメンバー ({len(candidates)}人)\n\n選ばれたのは... 🎉 {chosen_member.mention} 🎉 です！", color=discord.Color.red())
        view = MemberRouletteView(candidates)
        await interaction.edit_original_response(content=f"🎯 {chosen_member.mention} ロックオン！", embed=embed, view=view)

    @app_commands.command(name="koubun", description="ふたつの言葉からコウメ太夫のネタを生成します")
    @app_commands.describe(word1="〜かと思ったら", word2="〜でした")
    async def koubun(self, interaction: discord.Interaction, word1: str, word2: str):
        if len(word1) > 500 or len(word2) > 500:
            await interaction.response.send_message("❌ 文字数が長すぎます（各500文字以内にしてね）。", ephemeral=True)
            return
        joke = f"チャンチャカチャンチャン チャチャンチャチャンチャン♪\n\n**{word1}** かと思ったら〜〜〜\n\n**{word2}** でした〜〜〜\n\n**チクショーー！！** 😭"
        await interaction.response.send_message(joke)

    # --- テキストコマンド ---
    # access: all=誰でも / admin=サーバー管理者（ボットオーナーは権限不問） / owner=ボットオーナー専用
    # ※ !skr_help の一覧表示に使われます

    @commands.command(name="clear", extras={"access": "admin"})
    @prefix_admin_or_owner(manage_messages=True)
    async def msg_clear(self, ctx, amount: int):
        """【管理者/オーナー】直近のメッセージを指定件数まとめて削除します（1〜500）"""
        if amount < 1 or amount > 500:
            await ctx.send("❌ 1から500の間の数値を指定してください。", delete_after=5)
            return
        if ctx.guild and not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            await ctx.send("❌ Botに「メッセージの管理」権限がありません。", delete_after=5)
            return
        deleted = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"🧹 正常に {len(deleted) - 1} 件のメッセージを削除しました！", delete_after=5)

    @commands.command(name="servers", extras={"access": "owner"})
    @prefix_owner_only()
    async def list_servers(self, ctx):
        """【オーナー】Botが参加しているサーバー一覧を表示します"""
        guild_count = len(self.bot.guilds)
        server_list = ""
        for guild in self.bot.guilds:
            server_list += f"• **{guild.name}** (メンバー数: {guild.member_count}人, ID: `{guild.id}`)\n"
            if len(server_list) > 1800:
                server_list += "• ...他多数"
                break
        embed = discord.Embed(title="🤖 Bot所属サーバー一覧", description=f"現在 **{guild_count}** 個のサーバーに参加しています。\n\n{server_list}", color=discord.Color.blue())
        await ctx.send(embed=embed)

    @commands.command(name="say", extras={"access": "admin"})
    @prefix_admin_or_owner(administrator=True)
    async def say_message(self, ctx, channel: discord.TextChannel, *, message: str):
        """【管理者/オーナー】Botに指定チャンネルへ発言させます"""
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        await channel.send(message)

    @commands.command(name="status", extras={"access": "owner"})
    @prefix_owner_only()
    async def change_status(self, ctx, *, text: str):
        """【オーナー】Botのプレイ中ステータスを変更します"""
        await self.bot.change_presence(activity=discord.Game(name=text))
        await ctx.send(f"🤖 Botのステータスを「**{text} をプレイ中**」に変更しました！")

    @commands.command(name="ping", extras={"access": "all"})
    async def ping_check(self, ctx):
        """Botの応答速度を測定します"""
        raw_ping = self.bot.latency * 1000
        start_time = time.time()
        message = await ctx.send("⚡ Ping測定中...")
        end_time = time.time()
        msg_ping = (end_time - start_time) * 1000
        embed = discord.Embed(title="📶 Bot回線状態（レイテンシ）", color=discord.Color.green())
        embed.add_field(name="WebSocket Ping (APIとの通信)", value=f"`{raw_ping:.2f} ms`", inline=False)
        embed.add_field(name="Message Ping (メッセージ送信速度)", value=f"`{msg_ping:.2f} ms`", inline=False)
        status_text = "🟢 非常に快適" if raw_ping < 50 else "🟡 普通" if raw_ping < 150 else "🔴 遅延気味"
        embed.set_footer(text=f"稼働状況: {status_text}")
        await message.edit(content=None, embed=embed)

    @commands.command(name="restart", extras={"access": "owner"})
    @prefix_owner_only()
    async def restart_bot(self, ctx):
        """【オーナー】Botを終了します（自動再起動する環境では再起動になります）"""
        await ctx.send("**再起動処理を実行中 (プロセスを終了します)**")
        await self.bot.close()
        sys.exit(0)

# ⚙️ セットアップ
async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))