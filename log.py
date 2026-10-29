import discord
from discord import app_commands
import logging
import requests
import sys
import os

# ==========================================
# 1. ログ送信用の設定（このBotの#ログ用WebhookURLを設定）
# ==========================================
WEBHOOK_URL = "https://discord.com/api/webhooks/1565164095723016202/N7gStX4l7ggYD6mg-lw_Muc5-drQBL5MEVOXsBDs6gUicS5wZci9G6LPpwGU_lu02KKU"

class DiscordHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        if len(log_entry) > 1900:
            log_entry = log_entry[:1900] + "..."
        payload = {"content": f"```\n{log_entry}\n```"}
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=5)
        except Exception:
            pass

logger = logging.getLogger("BotManager")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = DiscordHandler()
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(asctime)s: %(message)s"))
    logger.addHandler(handler)


# ==========================================
# 2. 特定サーバー専用の再起動機能モジュール
# ==========================================
# 対象のサーバーIDを指定
TARGET_GUILD_ID = 1565155365207674970

def setup_restart_command(client: discord.Client, tree: app_commands.CommandTree):
    
    # pragma: ギルド（サーバー）を指定してコマンドを定義
    @tree.command(
        name="restart-bot", 
        description="このBotのプログラムを再起動します",
        guild=discord.Object(id=TARGET_GUILD_ID)
    )
    async def restart_bot(interaction: discord.Interaction):
        # 万が一、他のサーバーから何らかの手法で実行された場合の二重ガード
        if interaction.guild_id != TARGET_GUILD_ID:
            await interaction.response.send_message("```\n[ERROR] このサーバーではこのコマンドを実行できません。\n```", ephemeral=True)
            return

        # 正常に許可されたサーバーでの再起動処理
        await interaction.response.send_message("```\n[INFO] Botを再起動しています...\n```", ephemeral=True)
        logger.warning(f"指定サーバーでの操作により、Botを再起動します。")
        
        try:
            # 自分自身のプロセスを再起動する
            python = sys.executable
            os.execl(python, python, *sys.argv)
        except Exception as e:
            logger.error(f"再起動に失敗しました: {e}")