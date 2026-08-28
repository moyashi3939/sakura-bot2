import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
import time
import sys

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


# --- メインのCogクラス ---
class CommandsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- エラーハンドリング (テキストコマンド用) ---
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ このコマンドを実行する権限がありません。", delete_after=5)
        elif isinstance(error, commands.NotOwner):
            await ctx.send("❌ このコマンドはBot開発者のみ実行可能です。", delete_after=5)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ 引数が不足しています。使用方法を確認してください。", delete_after=5)

    # --- スラッシュコマンド (一般向け) ---

    @app_commands.command(name="omikuji", description="今日のおみくじを引きます")
    async def omikuji(self, interaction: discord.Interaction):
        fortunes = ["大吉 🌟", "吉 🎯", "中吉 😊", "小吉 👍", "末吉 🌿", "凶 👻", "半吉", "元凶", "大凶", "半凶", "大狂", "狂","勉強"]
        result = random.choice(fortunes)
        await interaction.response.send_message(f"{interaction.user.mention} さんの今日の運勢は... **{result}** です！")

    @app_commands.command(name="timer", description="指定した分数後にメンションで通知します（最大180分）")
    @app_commands.describe(minutes="タイマーの分数 (1〜180分)")
    async def timer(self, interaction: discord.Interaction, minutes: int):
        if minutes < 1 or minutes > 180:
            await interaction.response.send_message("❌ 1分から180分（3時間）の間で指定してください。", ephemeral=True)
            return
        await interaction.response.send_message(f"🔔 {minutes}分間のタイマーを開始しました。時間が来たらお知らせします！")
        await asyncio.sleep(minutes * 60)
        await interaction.channel.send(f"⏰ {interaction.user.mention} さん、{minutes}分が経過しました！")

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

    @commands.command(name="clear")
    @commands.has_permissions(manage_messages=True)
    async def msg_clear(self, ctx, amount: int):
        if amount < 1 or amount > 500:
            await ctx.send("❌ 1から500の間の数値を指定してください。", delete_after=5)
            return
        deleted = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"🧹 正常に {len(deleted) - 1} 件のメッセージを削除しました！", delete_after=5)

    @commands.command(name="servers")
    async def list_servers(self, ctx):
        if not await self.bot.is_owner(ctx.author):
            return

        guild_count = len(self.bot.guilds)
        server_list = ""
        for guild in self.bot.guilds:
            server_list += f"• **{guild.name}** (メンバー数: {guild.member_count}人, ID: `{guild.id}`)\n"
            if len(server_list) > 1800:
                server_list += "• ...他多数"
                break
        embed = discord.Embed(title="🤖 Bot所属サーバー一覧", description=f"現在 **{guild_count}** 個のサーバーに参加しています。\n\n{server_list}", color=discord.Color.blue())
        await ctx.send(embed=embed)

    @commands.command(name="say")
    @commands.has_permissions(administrator=True)
    async def say_message(self, ctx, channel: discord.TextChannel, *, message: str):
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass
        await channel.send(message)

    @commands.command(name="status")
    async def change_status(self, ctx, *, text: str):
        if not await self.bot.is_owner(ctx.author):
            return

        await self.bot.change_presence(activity=discord.Game(name=text))
        await ctx.send(f"🤖 Botのステータスを「**{text} をプレイ中**」に変更しました！")

    @commands.command(name="ping")
    async def ping_check(self, ctx):
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

    @commands.command(name="restart")
    async def restart_bot(self, ctx):
        if not await self.bot.is_owner(ctx.author):
            return

        await ctx.send("**再起動処理を実行中 (プロセスを終了します)**")
        await self.bot.close()
        sys.exit(0)

# ⚙️ セットアップ
async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))