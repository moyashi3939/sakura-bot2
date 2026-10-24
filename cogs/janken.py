import asyncio
import logging
import os
import random
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool

logger = logging.getLogger(__name__)

# ----------------------------------------------------
# 設定・定数
# ----------------------------------------------------
# DATABASE_URL2 があればじゃんけん専用DB、無ければ共有DB(DATABASE_URL3)を使う
DATABASE_URL2 = os.getenv("DATABASE_URL2")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS janken_stats (
    user_id BIGINT PRIMARY KEY,
    wins INT NOT NULL DEFAULT 0,
    losses INT NOT NULL DEFAULT 0,
    draws INT NOT NULL DEFAULT 0
);
"""

HANDS = {"rock": "✊", "scissors": "✌️", "paper": "🖐️"}
HAND_NAMES = {"rock": "グー", "scissors": "チョキ", "paper": "パー"}


# ----------------------------------------------------
# DB処理ヘルパー関数（Poolを使用）
# ----------------------------------------------------
async def record_result(pool: asyncpg.Pool, user: discord.User | discord.Member, result: str):
    """勝敗結果をNeon DBに記録/更新する（Botは除外）"""
    # ユーザーが存在しない、またはBotの場合は記録しない
    if user is None or user.bot:
        return

    async with pool.acquire() as conn:
        if result == "win":
            query = """
                INSERT INTO janken_stats (user_id, wins, losses, draws)
                VALUES ($1, 1, 0, 0)
                ON CONFLICT (user_id) DO UPDATE
                SET wins = janken_stats.wins + 1;
            """
        elif result == "loss":
            query = """
                INSERT INTO janken_stats (user_id, wins, losses, draws)
                VALUES ($1, 0, 1, 0)
                ON CONFLICT (user_id) DO UPDATE
                SET losses = janken_stats.losses + 1;
            """
        elif result == "draw":
            query = """
                INSERT INTO janken_stats (user_id, wins, losses, draws)
                VALUES ($1, 0, 0, 1)
                ON CONFLICT (user_id) DO UPDATE
                SET draws = janken_stats.draws + 1;
            """
        await conn.execute(query, user.id)


async def get_top_rankings(pool: asyncpg.Pool, limit: int = 10):
    """
    合計得点（勝ち2点, 引き分け1点, 負け-1点）でソートしてランキングを取得する。
    同点の場合は勝利数が多い順にソート。
    """
    async with pool.acquire() as conn:
        query = """
            SELECT 
                user_id, 
                wins, 
                losses, 
                draws,
                (wins * 2 + draws * 1 - losses * 1) AS points
            FROM janken_stats
            ORDER BY points DESC, wins DESC
            LIMIT $1;
        """
        rows = await conn.fetch(query, limit)
        return rows


# ----------------------------------------------------
# じゃんけん View
# ----------------------------------------------------
class JankenView(discord.ui.View):

    def __init__(
        self,
        challenger: discord.Member,
        opponent: discord.Member,
        is_bot: bool,
        pool: asyncpg.Pool,
        timeout: float = 60.0,
    ):
        super().__init__(timeout=timeout)

        self.challenger = challenger
        self.opponent = opponent
        self.is_bot = is_bot
        self.pool = pool
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()  # 2人がほぼ同時に押しても判定が1回だけになるように

        self.choices = {
            challenger.id: None,
            opponent.id: None,
        }

        # Bot対戦の場合はBotの手をあらかじめランダム決定
        if is_bot:
            self.choices[opponent.id] = random.choice(list(HANDS.keys()))

    async def on_timeout(self):
        """タイムアウト（放置）時の処理"""
        for item in self.children:
            item.disabled = True
        
        if self.message:
            try:
                await self.message.edit(
                    content=f"⏰ 制限時間（{int(self.timeout or 60)}秒）が切れたため、対戦はキャンセルされました。",
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="✊ グー",
        style=discord.ButtonStyle.primary,
        custom_id="janken_rock",
    )
    async def rock_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.process_choice(interaction, "rock")

    @discord.ui.button(
        label="✌️ チョキ",
        style=discord.ButtonStyle.success,
        custom_id="janken_scissors",
    )
    async def scissors_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.process_choice(interaction, "scissors")

    @discord.ui.button(
        label="🖐️ パー",
        style=discord.ButtonStyle.danger,
        custom_id="janken_paper",
    )
    async def paper_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.process_choice(interaction, "paper")

    async def process_choice(self, interaction: discord.Interaction, hand: str):
        try:
            async with self._lock:
                await self._process_choice(interaction, hand)
        except Exception:
            logger.exception("じゃんけんの処理中にエラー")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ エラーが発生しました。", ephemeral=True)

    async def _process_choice(self, interaction: discord.Interaction, hand: str):
        user = interaction.user

        if user.id not in self.choices:
            await interaction.response.send_message(
                "❌ あなたはこの対戦のプレイヤーではありません！",
                ephemeral=True,
            )
            return

        if self.choices[user.id] is not None:
            await interaction.response.send_message(
                "❌ 既に手を選んでいます！", ephemeral=True
            )
            return

        self.choices[user.id] = hand

        if None in self.choices.values():
            await interaction.response.send_message(
                f"✅ {HAND_NAMES[hand]} を選択しました！\n相手の入力待ちです。",
                ephemeral=True,
            )
            return

        await self.judge(interaction)

    async def judge(self, interaction: discord.Interaction):
        try:
            p1_hand = self.choices[self.challenger.id]
            p2_hand = self.choices[self.opponent.id]

            p1_emoji = HANDS[p1_hand]
            p2_emoji = HANDS[p2_hand]

            embed = discord.Embed(
                title="じゃんけん ポン！", color=discord.Color.blue()
            )

            embed.add_field(
                name=self.challenger.display_name,
                value=f"{p1_emoji} {HAND_NAMES[p1_hand]}",
                inline=True,
            )

            embed.add_field(
                name=self.opponent.display_name,
                value=f"{p2_emoji} {HAND_NAMES[p2_hand]}",
                inline=True,
            )

            # ボタンを無効化
            for item in self.children:
                item.disabled = True

            # --- あいこ処理 ---
            if p1_hand == p2_hand:
                embed.color = discord.Color.orange()
                embed.description = (
                    "🤔 **あいこでしょ！**\n\n引き分けです！\n"
                    "続ける場合はもう一度 `/janken play` を実行してください。"
                )

                # DBに引き分けを記録（Botは内部処理で弾かれます）
                await record_result(self.pool, self.challenger, "draw")
                await record_result(self.pool, self.opponent, "draw")

                await interaction.response.edit_message(
                    content="引き分け！", embed=embed, view=self
                )
                self.stop()
                return

            # --- 勝敗判定 ---
            win_conditions = [
                ("rock", "scissors"),
                ("scissors", "paper"),
                ("paper", "rock"),
            ]

            if (p1_hand, p2_hand) in win_conditions:
                winner, loser = self.challenger, self.opponent
            else:
                winner, loser = self.opponent, self.challenger

            embed.color = discord.Color.green()
            embed.description = f"🎉 **勝者: {winner.mention} !!**"

            # DBに勝敗を記録（Botは内部処理で弾かれます）
            await record_result(self.pool, winner, "win")
            await record_result(self.pool, loser, "loss")

            await interaction.response.edit_message(
                content="対戦終了！", embed=embed, view=self
            )
            self.stop()

        except Exception:
            logger.exception("じゃんけんの判定中にエラー")


# ----------------------------------------------------
# じゃんけん Cog（グループコマンド構成）
# ----------------------------------------------------
class JankenCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pool: asyncpg.Pool | None = None
        self._own_pool = False

    async def cog_load(self):
        """DB接続を用意し、テーブルが無ければ作成する"""
        if DATABASE_URL2:
            self.pool = await asyncpg.create_pool(DATABASE_URL2, statement_cache_size=0)
            self._own_pool = True
        else:
            self.pool = await get_pool(self.bot)  # 共有プール
            self._own_pool = False
        async with self.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

    async def cog_unload(self):
        """専用プールの場合のみ閉じる（共有プールは閉じない）"""
        if getattr(self, "_own_pool", False) and self.pool:
            await self.pool.close()

    janken_group = app_commands.Group(
        name="janken", description="じゃんけんコマンド一覧"
    )

    # 1. `/janken play` (じゃんけん対戦)
    @janken_group.command(
        name="play",
        description="サーバーの誰か、またはBotとじゃんけん対決をします！",
    )
    @app_commands.describe(
        相手="対戦したいメンバーを選んでね（選ばないとBotと対戦になります）"
    )
    async def play(
        self, interaction: discord.Interaction, 相手: discord.Member = None
    ):
        try:
            challenger = interaction.user

            if interaction.guild is None:
                await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
                return

            if (
                相手 is not None
                and 相手.bot
                and 相手.id != self.bot.user.id
            ):
                await interaction.response.send_message(
                    "❌ 他のBotとじゃんけんすることはできません！",
                    ephemeral=True,
                )
                return

            if 相手 is None or 相手.id == self.bot.user.id:
                opponent = self.bot.user
                is_bot = True
            elif 相手.id == challenger.id:
                await interaction.response.send_message(
                    "❌ 自分自身とじゃんけんはできません！",
                    ephemeral=True,
                )
                return
            else:
                opponent = 相手
                is_bot = False

            embed = discord.Embed(
                title="⚔️ じゃんけん勝負勃発！",
                description=(
                    f"{challenger.mention} **vs** {opponent.mention}\n\n"
                    "下のボタンを押して手を選んでください！"
                ),
                color=discord.Color.blurple(),
            )

            view = JankenView(
                challenger=challenger,
                opponent=opponent,
                is_bot=is_bot,
                pool=self.pool,
            )

            await interaction.response.send_message(
                content=f"{challenger.mention} vs {opponent.mention}",
                embed=embed,
                view=view,
            )
            # タイムアウト処理のためにMessageオブジェクトを保持
            view.message = await interaction.original_response()

        except Exception:
            logger.exception("/janken play でエラー")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ エラーが発生しました。", ephemeral=True)

    # 2. `/janken ranking` (ポイント順ランキング表示)
    @janken_group.command(
        name="ranking", description="じゃんけんポイントランキングを表示します！"
    )
    async def ranking(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            rows = await get_top_rankings(self.pool, limit=10)

            if not rows:
                await interaction.followup.send(
                    "📊 まだ対戦データが存在しません！じゃんけんをして記録を作ろう！"
                )
                return

            embed = discord.Embed(
                title="🏆 じゃんけんポイントランキング (TOP 10)",
                description="※得点計算: 勝ち **2点** / 引き分け **1点** / 負け **-1点**",
                color=discord.Color.gold(),
            )

            rank_text = ""
            for i, row in enumerate(rows, start=1):
                user_id = row["user_id"]
                wins = row["wins"]
                losses = row["losses"]
                draws = row["draws"]
                points = row["points"]

                member = interaction.guild.get_member(user_id) if interaction.guild else None
                user_name = member.display_name if member else f"<@{user_id}>"

                badge = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`#{i:2d}`")

                rank_text += (
                    f"{badge} **{user_name}** — **{points} Pt**\n"
                    f"┗ `{wins}勝` / `{draws}分` / `{losses}敗`\n\n"
                )

            embed.add_field(name="順位一覧", value=rank_text, inline=False)
            embed.set_footer(text="対戦を重ねてポイントをためよう！")

            await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        except Exception:
            logger.exception("ランキング取得エラー")
            await interaction.followup.send(
                "❌ ランキングの取得中にエラーが発生しました。"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(JankenCog(bot))