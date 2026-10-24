import asyncio
import os
import time
import traceback
import discord
from discord import app_commands
from discord.ext import commands
import inspect
import unicodedata

# ── 🔴 クイズ早押しView ──
def normalize_answer(text: str) -> str:
    """全角/半角・大文字小文字・空白の違いを無視して比較できるようにする"""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


class QuizBuzzerView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        answer: str,
        start_time: float,
        embed: discord.Embed,
        quiz_timeout: float = 900.0,
        answer_timeout: float = 15.0,
        author_id: int | None = None,
    ):
        super().__init__(timeout=quiz_timeout)
        self.author_id = author_id  # 出題者は解答不可
        self.bot = bot
        self.answer = answer.strip()
        self.start_time = start_time
        self.base_embed = embed
        self.is_processing = False
        self.wrong_users = set()
        self.quiz_message = None
        self.answer_timeout = answer_timeout

    @discord.ui.button(label="押しボタン 🔴", style=discord.ButtonStyle.danger)
    async def press_buzzer(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.is_processing:
            await interaction.response.send_message(
                "遅かった！すでに他の人がボタンを押しています。", ephemeral=True
            )
            return
        if interaction.user.id == self.author_id:
            await interaction.response.send_message(
                "❌ 出題者は解答できません。", ephemeral=True
            )
            return
        if interaction.user.id in self.wrong_users:
            await interaction.response.send_message(
                "❌ あなたはすでに解答権を失っています（お手付き）。", ephemeral=True
            )
            return

        self.is_processing = True
        button.disabled = True
        button.style = discord.ButtonStyle.secondary
        button.label = "考え中... 💬"
        await interaction.response.edit_message(view=self)

        elapsed_time = time.time() - self.start_time
        announce_msg = await interaction.channel.send(
            f"📢 **早押し成功！** （タイム: `{elapsed_time:.2f}秒` ⏱️）\n"
            f"解答権： {interaction.user.mention} さん！\n🚨 **{int(self.answer_timeout)}秒以内**に答えを入力してください！"
        )

        def check_answer(msg):
            return (
                msg.author == interaction.user
                and msg.channel == interaction.channel
            )

        try:
            user_msg = await self.bot.wait_for(
                "message", check=check_answer, timeout=self.answer_timeout
            )
            if normalize_answer(user_msg.content) == normalize_answer(self.answer):
                await user_msg.reply(
                    f"🎉 **正解！！**\n答えは「**{self.answer}**」でした！"
                )
                self.stop()
                button.label = "正解が出ました 🎉"
                button.style = discord.ButtonStyle.success
                try:
                    await self.quiz_message.edit(view=self)
                except discord.HTTPException:
                    pass
                return
            else:
                await user_msg.reply(
                    f"❌ **不正解！** {interaction.user.mention} さんは解答権を失いました。"
                )
        except asyncio.TimeoutError:
            await interaction.channel.send(
                f"⏰ タイムアップ！ {interaction.user.mention} さんは時間切れです。"
            )

        self.wrong_users.add(interaction.user.id)
        self.is_processing = False
        button.disabled = False
        button.style = discord.ButtonStyle.danger
        button.label = "押しボタン 🔴"

        lost_mentions = [f"<@{uid}>" for uid in self.wrong_users]
        self.base_embed.set_footer(
            text=f"※解答権喪失: {', '.join(lost_mentions)}\nまだの人はボタンを押せます！"
        )
        try:
            await self.quiz_message.edit(embed=self.base_embed, view=self)
            await announce_msg.delete()
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
            item.label = "時間切れ ⏰"
            item.style = discord.ButtonStyle.secondary

        if self.quiz_message:
            try:
                await self.quiz_message.edit(view=self)
                await self.quiz_message.reply(
                    f"⏰ **時間切れでクイズ終了です！**\n正解は「**{self.answer}**」でした！"
                )
            except discord.HTTPException:
                pass


# ── 👑 クイズ本体の Cog (QuizCog) ──
class QuizCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def get_quiz_settings_from_settings_cog(
        self, guild_id: int
    ) -> tuple[float, float]:
        """SettingsCog からクイズのタイムアウト設定を取得する"""
        settings_cog = self.bot.get_cog("SettingsCog")
        if settings_cog:
            try:
                if hasattr(settings_cog, "get_quiz_settings"):
                    # SettingsCogに get_quiz_settings(guild_id) がある場合
                    res = getattr(settings_cog, "get_quiz_settings")(guild_id)
                    if inspect.iscoroutine(res):
                        res = await res
                    return res.get("quiz_timeout", 900.0), res.get("answer_timeout", 15.0)
            except Exception as e:
                print(f"SettingsCog からのクイズ設定取得エラー: {e}")

        # 設定が見つからない/取得失敗時はデフォルト値
        return 900.0, 15.0

    @app_commands.command(name="quiz", description="早押しクイズを出題します！")
    @app_commands.guild_only()
    @app_commands.describe(
        問題="クイズの問題文を入力してください",
        答え="クイズの正解を入力してください"
    )
    async def quiz(self, interaction: discord.Interaction, 問題: str, 答え: str):
        try:
            # /setting quiz で専用チャンネルが設定されていれば、そこでのみ出題できる
            settings_cog = self.bot.get_cog("SettingsCog")
            if settings_cog:
                quiz_ch = (await settings_cog.get_quiz_config(interaction.guild_id)).get("quiz_channel_id")
                if quiz_ch and interaction.channel_id != quiz_ch:
                    await interaction.response.send_message(
                        f"❌ クイズは <#{quiz_ch}> で出題してください。", ephemeral=True
                    )
                    return
            if len(問題) > 1500:
                await interaction.response.send_message("❌ 問題文が長すぎます（1500文字以内）。", ephemeral=True)
                return

            embed = discord.Embed(
                title="❓ 早押しクイズ！",
                description=f"**問題：**\n{問題} ({interaction.user.mention})",
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="下の赤いボタンを押して解答権を獲得してください！")

            # SettingsCog から設定を取得
            if interaction.guild_id:
                quiz_timeout, answer_timeout = (
                    await self.get_quiz_settings_from_settings_cog(
                        interaction.guild_id
                    )
                )
            else:
                quiz_timeout, answer_timeout = 900.0, 15.0

            view = QuizBuzzerView(
                bot=self.bot,
                answer=答え,
                start_time=time.time(),
                embed=embed,
                quiz_timeout=quiz_timeout,
                answer_timeout=answer_timeout,
                author_id=interaction.user.id,
            )

            await interaction.response.send_message(
                "📢 クイズを出題しました！", ephemeral=True
            )
            quiz_message = await interaction.channel.send(
                embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            view.quiz_message = quiz_message

        except Exception as e:
            traceback.print_exc()
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ クイズの作成中にエラーが発生しました。", ephemeral=True
                )


# ── 🚀 setup 関数 ──
async def setup(bot: commands.Bot):
    await bot.add_cog(QuizCog(bot))