import datetime
import logging
import os
import re
import traceback
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils.db import get_pool
from utils.perms import format_permission_names

load_dotenv()
TOKEN = os.getenv("token") or os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN")

# .env に OWNER_IDS=123456789,987654321 と書くと、アプリのオーナー以外もボットオーナー扱いにできます
EXTRA_OWNER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("OWNER_IDS", "")) if x.strip().isdigit()
}

intents = discord.Intents.all()
logger = logging.getLogger("sakura")


def setup_logging() -> None:
    """コンソール + logs/bot.log（ローテーション）にログを出す"""
    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:  # 二重登録防止（再実行時など）
        return

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        "logs/bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


class Blacklisted(app_commands.CheckFailure):
    """ブラックリスト登録されたユーザー / サーバーからの実行"""


class BotTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: "MyBot" = interaction.client  # type: ignore[assignment]
        if await bot.is_owner(interaction.user):
            return True
        if bot.is_blacklisted(interaction.user.id, interaction.guild_id):
            raise Blacklisted()
        return True


class MyBot(commands.Bot):

    def __init__(self):
        super().__init__(
            command_prefix="!skr_",
            intents=intents,
            help_command=None,  # 独自の !skr_help / /help を使う
            tree_cls=BotTree,
        )
        self.db_pool = None
        self.started_at = datetime.datetime.now(datetime.timezone.utc)

        # ブラックリスト（owner.py が DB から読み込む）: {id: 理由}
        self.blacklist_users: dict[int, str] = {}
        self.blacklist_guilds: dict[int, str] = {}

        # プレフィックスコマンド全体に対するチェック
        self.add_check(self._global_command_check)

    # ------------------------------------------------------------
    # ボットオーナー判定（アプリのオーナー/Team + OWNER_IDS）
    # ------------------------------------------------------------
    async def is_owner(self, user: discord.abc.User) -> bool:
        if user.id in EXTRA_OWNER_IDS:
            return True
        return await super().is_owner(user)

    def is_blacklisted(self, user_id: int, guild_id: int | None = None) -> bool:
        if user_id in self.blacklist_users:
            return True
        return guild_id is not None and guild_id in self.blacklist_guilds

    async def _global_command_check(self, ctx: commands.Context) -> bool:
        if await self.is_owner(ctx.author):
            return True
        if self.is_blacklisted(ctx.author.id, ctx.guild.id if ctx.guild else None):
            raise commands.CheckFailure("blacklisted")
        return True

    # ------------------------------------------------------------
    # 起動 / 終了
    # ------------------------------------------------------------
    async def setup_hook(self):
        # スラッシュコマンドのエラーハンドラを設定
        self.tree.on_error = self.on_tree_error

        # 共有DBプールを最初に作成（各Cogはこれを使う）
        if os.getenv("DATABASE_URL3"):
            try:
                await get_pool(self)
            except Exception:
                logger.exception("❌ DB接続に失敗しました（DBを使うCogは読み込みに失敗します）")

        # Cogの自動読み込み（読み込み順を固定するため sorted）
        cogs_dir = "./cogs"
        if os.path.exists(cogs_dir):
            for filename in sorted(os.listdir(cogs_dir)):
                if filename.endswith(".py") and not filename.startswith("_"):
                    cog_name = f"cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(cog_name)
                        logger.info("✅ %s を読み込みました", cog_name)
                    except Exception:
                        logger.exception("❌ %s の読み込みに失敗しました", cog_name)

        # スラッシュコマンドの同期処理
        try:
            synced = await self.tree.sync()
            logger.info("✅ %d 個のスラッシュコマンドを同期しました", len(synced))
        except Exception:
            logger.exception("❌ スラッシュコマンドの同期に失敗しました")

    async def close(self):
        await super().close()
        if self.db_pool is not None:
            try:
                await self.db_pool.close()
            except Exception:
                pass

    async def on_ready(self):
        """Botがログインしたときに動く処理"""
        logger.info("Logged in as %s (%s)", self.user.name, self.user.id)
        logger.info("--------------------------------------------")

    async def on_guild_join(self, guild: discord.Guild):
        logger.info("参加: %s (ID: %s, メンバー: %s)", guild.name, guild.id, guild.member_count)
        if guild.id in self.blacklist_guilds:
            logger.warning("ブラックリスト登録済みのサーバーのため退出します: %s (%s)", guild.name, guild.id)
            await guild.leave()

    # ------------------------------------------------------------
    # ログ・メッセージ処理
    # ------------------------------------------------------------
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.application_command:
            command_name = interaction.command.qualified_name if interaction.command else "Unknown"
            guild = interaction.guild.name if interaction.guild else "DM"
            logger.info(
                "[COMMAND] %s (ID: %s) ran /%s in Server: %s",
                interaction.user, interaction.user.id, command_name, guild,
            )

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        # テキストコマンド（!skr_）を受け付けるための必須処理
        await self.process_commands(message)

    # ------------------------------------------------------------
    # エラーハンドリング
    # ------------------------------------------------------------
    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        command_name = interaction.command.qualified_name if interaction.command else "Unknown"

        if isinstance(error, Blacklisted):
            text = "🚫 あなた（またはこのサーバー）はBotの利用が制限されています。"
        elif isinstance(error, app_commands.MissingPermissions):
            text = f"❌ このコマンドを実行するには {format_permission_names(error.missing_permissions)} の権限が必要です。"
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "❌ このコマンドはサーバー内でのみ使用できます。"
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"⏳ クールダウン中です。{error.retry_after:.1f}秒後にもう一度お試しください。"
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = f"❌ Botに {format_permission_names(error.missing_permissions)} の権限がありません。"
        elif isinstance(error, app_commands.CheckFailure):
            text = "❌ このコマンドを実行する条件を満たしていません。"
        else:
            original = getattr(error, "original", error)
            logger.error(
                "[ERROR] %s (ID: %s) failed to run /%s: %r",
                interaction.user, interaction.user.id, command_name, original,
                exc_info=(type(original), original, original.__traceback__),
            )
            text = "❌ コマンドの実行中にエラーが発生しました。"

        try:
            if interaction.response.is_done():
                # 他のハンドラがすでに返答済みの権限エラーなどは二重送信しない
                if isinstance(error, app_commands.CheckFailure):
                    return
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """通常のテキストコマンド（!skr_）でエラーが出た場合"""
        if ctx.command and hasattr(ctx.command, "on_error"):
            return  # コマンド個別のハンドラがある場合はそちらに任せる

        error = getattr(error, "original", error)

        # 存在しないコマンド・オーナー専用コマンドは無視（ログを汚さない / 存在を悟らせない）
        if isinstance(error, (commands.CommandNotFound, commands.NotOwner)):
            return
        if isinstance(error, commands.CheckFailure) and str(error) == "blacklisted":
            return

        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                f"❌ このコマンドを実行するには {format_permission_names(error.missing_permissions)} の権限が必要です。",
                delete_after=8,
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ このコマンドはサーバー内でのみ使用できます。", delete_after=8)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ 引数が不足しています: `{error.param.name}`", delete_after=8)
        elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
            await ctx.send(f"❌ 引数が正しくありません: {error}", delete_after=8)
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ {error.retry_after:.1f}秒後にもう一度お試しください。", delete_after=5)
        elif isinstance(error, commands.CheckFailure):
            return
        else:
            logger.error(
                "テキストコマンド %s でエラー: %r", ctx.command, error,
                exc_info=(type(error), error, error.__traceback__),
            )


def run() -> None:
    setup_logging()

    if not TOKEN:
        # ❌ そもそも .env のトークンが読み込めていない場合
        with open("error_log.txt", "w", encoding="utf-8") as f:
            f.write(
                "❌ エラー: .env ファイルから 'token' を読み込めませんでした。"
                "ファイルが存在しないか、中身が空の可能性があります。"
            )
        logger.error("エラー: .env から 'token' が読み込めませんでした。")
        return

    bot = MyBot()
    try:
        bot.run(TOKEN, log_handler=None)  # ログは setup_logging() で設定済み
    except Exception as e:
        # ❌ 起動時に何らかのエラーで落ちた場合、内容を書き出す
        with open("error_log.txt", "w", encoding="utf-8") as f:
            f.write("⚠️ 起動エラーが発生しました:\n")
            f.write(traceback.format_exc())
        raise e


if __name__ == "__main__":
    run()

import logging
import requests

# ==========================================
# 1. Discord Webhookへの送信設定
# ==========================================
WEBHOOK_URL = "https://discord.com/api/webhooks/1565164095723016202/N7gStX4l7ggYD6mg-lw_Muc5-drQBL5MEVOXsBDs6gUicS5wZci9G6LPpwGU_lu02KKU"

class DiscordHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        # Discordの文字数制限（2000文字）対策
        if len(log_entry) > 1900:
            log_entry = log_entry[:1900] + "..."
            
        # エラーのときは強調表示をつける
        prefix = "🚨 **エラー発生**\n" if record.levelno >= logging.ERROR else ""
        payload = {"content": f"{prefix}```\n{log_entry}\n```"}
        
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=5)
        except Exception:
            pass

# ロガーの初期設定
logger = logging.getLogger("BotLogger")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = DiscordHandler()
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(asctime)s: %(message)s"))
    logger.addHandler(handler)