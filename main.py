import os
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import traceback

load_dotenv()
TOKEN = os.getenv("token")

intents = discord.Intents.all()

class MyBot(commands.Bot):

    def __init__(self):

        super().__init__(command_prefix="!skr_", intents=intents)

        self.remove_command("help")

    async def setup_hook(self):
        # スラッシュコマンドのエラーハンドラを設定
        self.tree.on_error = self.on_tree_error
        
        # Cogの自動読み込み
        cogs_dir = "./cogs"
        if os.path.exists(cogs_dir):
            for filename in os.listdir(cogs_dir):
                if filename.endswith(".py"):
                    cog_name = f"cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(cog_name)
                        print(f"✅ {cog_name} を読み込みました")
                    except Exception as e:
                        print(f"❌ {cog_name} の読み込みに失敗しました: {e}")

        # 💡 【追加】スラッシュコマンドの同期処理
        try:
            synced = await self.tree.sync()
            print(f"✅ {len(synced)} 個のスラッシュコマンドを同期しました")
        except Exception as e:
            print(f"❌ スラッシュコマンドの同期に失敗しました: {e}")

        # 💡 【重要】再起動後もロールパネルのボタンを反応させるための設定
        role_cog = self.get_cog("RolePanelCog")
        if role_cog and hasattr(role_cog, "role_view"):
            self.add_view(role_cog.role_view)

    # 💡 すべてのインタラクション（スラッシュコマンド等）を検知するリスナー
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.application_command:
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            user = interaction.user
            command_name = interaction.command.name if interaction.command else "Unknown"
            guild = interaction.guild.name if interaction.guild else "DM"
            
            print(f"[{now}] [COMMAND] {user} (ID: {user.id}) ran /{command_name} in Server: {guild}")

    # 💡 【重要】テキストコマンド（!skr_）を正常に受け付けるための必須処理！
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        # これを呼び出すことで、クラス外で定義した @bot.command が正常に動くようになります
        await self.process_commands(message)

    # 💡 スラッシュコマンドでエラーが出た時のハンドラ（1つに綺麗に統合しました）
    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        user = interaction.user
        command_name = interaction.command.name if interaction.command else "Unknown"
        
        print(f"[{now}] [ERROR] {user} (ID: {user.id}) failed to run /{command_name}: {error}")
        
        # ユーザーにもエラーを通知（すでに返答済みの場合はスキップ）
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ コマンドの実行中にエラーが発生したか、権限が足りません。", ephemeral=True)

    async def on_ready(self):
        """Botがログインしたときに動く処理"""
        print(f"Logged in as {self.user.name} ({self.user.id})")
        print("--------------------------------------------")

bot = MyBot()

# --- テキストコマンド（管理用・オーナー限定） ---

@bot.command(name="sync")
@commands.is_owner() # オーナー以外が叩いても反応しない
async def sync_commands(ctx):
    """【オーナー限定】スラッシュコマンドをDiscordと同期します"""
    message = await ctx.send("⚡ スラッシュコマンドを同期中...")
    try:
        synced = await bot.tree.sync()
        await message.edit(content=f"✅ {len(synced)}個のスラッシュコマンドをグローバル同期しました！\n（反映に数分〜最大1時間かかる場合があります）")
    except Exception as e:
        await message.edit(content=f"❌ 同期エラーが発生しました: {e}")

# --- エラーハンドリング ---

@bot.event
async def on_command_error(ctx, error):
    """通常のテキストコマンドでエラーが出た場合"""
    if isinstance(error, (commands.NotOwner, commands.CommandNotFound, commands.MissingPermissions)):
        return  # 権限不足や存在しないコマンドは無視（ログを汚さない）
    raise error

# --- 起動処理 ---
if TOKEN:
    try:
        bot.run(TOKEN)
    except Exception as e:
        # ❌ 起動時に何らかのエラーで落ちた場合、内容を書き出す
        with open("error_log.txt", "w", encoding="utf-8") as f:
            f.write("⚠️ 起動エラーが発生しました:\n")
            f.write(traceback.format_exc())
        raise e
else:
    # ❌ そもそも .env のトークンが読み込めていない場合
    with open("error_log.txt", "w", encoding="utf-8") as f:
        f.write("❌ エラー: .env ファイルから 'token' を読み込めませんでした。ファイルが存在しないか、中身が空の可能性があります。")
    print("エラー: .env から 'token' が読み込めませんでした。")