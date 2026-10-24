import json
import logging
import re
import time
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


# 📯 挨拶機能のコグ
class GreetingsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_greet_times = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # /setting features で挨拶がOFFのサーバーでは反応しない
        settings_cog = self.bot.get_cog("SettingsCog")
        if settings_cog and not await settings_cog.is_feature_enabled(message.guild.id, "greetings"):
            return

        channel_id = message.channel.id
        content = message.content
        lower_content = content.lower().strip()
        current_time = time.time()

        ohayo_words = [
            "おは",
            "おはよ",
            "おはよう",
            "ぐっどもーにんぐ",
            "グッドモーニング",
            "good morning",
        ]

        if any(word in lower_content for word in ohayo_words):
            last_time = self.last_greet_times.get(channel_id, 0)
            if current_time - last_time >= 900.0:
                await message.channel.send(
                    f"おはようございます、{message.author.display_name}さん！今日も頑張りましょう！☀️"
                )
                self.last_greet_times[channel_id] = current_time
            return

        if "おやすみ" in content:
            last_time = self.last_greet_times.get(channel_id, 0)
            if current_time - last_time >= 900.0:
                await message.channel.send(
                    f"おやすみなさい、{message.author.display_name}さん。良い夢を！🌙"
                )
                self.last_greet_times[channel_id] = current_time
            return

        ya_words = ["や", "やぁ", "ya", "yala", "yaxa"]
        if lower_content in ya_words:
            last_time = self.last_greet_times.get(channel_id, 0)
            if current_time - last_time >= 900.0:
                await message.channel.send("やぁ")
                self.last_greet_times[channel_id] = current_time
            return

        yo_words = ["よ", "よぁ", "yo", "yoxo", "yolo"]
        if lower_content in yo_words:
            last_time = self.last_greet_times.get(channel_id, 0)
            if current_time - last_time >= 900.0:
                await message.channel.send("よぉ")
                self.last_greet_times[channel_id] = current_time
            return


# 🛡️ モデレーション機能のコグ（冷笑削除）
class ModerationCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ng_words = [
            "おおw", "うおw", "oow", "uow",
            "おおｗ", "うおｗ", "うお", "uo",
            "どわーｗ", "どわーw", "どわ-", "どわ-w",
            "dowa-w", "dowa-", "dowaーw",
            "きちーｗ", "きちーw", "kichi-w", "kiti-w",
        ]

    def _legacy_channels(self) -> list[int]:
        """旧仕様の bot_config.json ("reishou": {"channels": [...]}) も引き続き有効にする"""
        try:
            with open("bot_config.json", "r", encoding="utf-8") as f:
                return [int(c) for c in json.load(f).get("reishou", {}).get("channels", [])]
        except (OSError, ValueError):
            return []

    async def get_config(self, guild_id: int) -> tuple[list[int], int | None]:
        """(冷笑削除の対象チャンネルIDのリスト, モデレーションログのチャンネルID) を返す。
        自動モデレーションがOFFのサーバーでは対象なし。"""
        channels = self._legacy_channels()
        mod_log = None

        settings_cog = self.bot.get_cog("SettingsCog")
        if settings_cog:
            try:
                config = await settings_cog.get_moderation_config(guild_id)
                if not config.get("auto_mod_enabled", True):
                    return [], config.get("mod_log_channel_id")
                if config.get("reishou_channel_id"):
                    channels.append(int(config["reishou_channel_id"]))
                mod_log = config.get("mod_log_channel_id")
            except Exception as e:
                logger.warning(f"モデレーション設定の取得中にエラーが発生しました: {e}")
        return channels, mod_log

    async def _log(self, guild: discord.Guild, channel_id: int | None, embed: discord.Embed):
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel) and channel.permissions_for(guild.me).send_messages:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.channel:
            return

        allowed_channels, mod_log_id = await self.get_config(message.guild.id)

        if not allowed_channels or (message.channel.id not in allowed_channels):
            return

        content = message.content.lower()

        for ng_word in self.ng_words:
            target = ng_word.lower()
            # 全角スペースや半角スペースを考慮したパターン
            pattern = rf"(^|[\s\u3000]){re.escape(target)}($|[\s\u3000])"

            if content == target or re.search(pattern, content):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"⚠️ 冷笑を検知し、**{message.author.display_name}** のメッセージを削除しました！"
                    )
                    log_embed = discord.Embed(
                        title="🛡️ 冷笑を自動削除しました",
                        description=f"{message.author.mention} in {message.channel.mention}",
                        color=discord.Color.orange(),
                    )
                    log_embed.add_field(name="内容", value=discord.utils.escape_markdown(message.content)[:1000] or "(なし)")
                    await self._log(message.guild, mod_log_id, log_embed)
                    return
                except discord.Forbidden:
                    logger.warning(
                        f"[{message.guild.name}] 権限が足りないため削除できませんでした。"
                    )
                    return
                except Exception as e:
                    logger.error(f"冷笑削除処理中にエラーが発生しました: {e}")
                    return


# ⚙️ コグをBotに登録する
async def setup(bot: commands.Bot):
    await bot.add_cog(GreetingsCog(bot))
    await bot.add_cog(ModerationCog(bot))