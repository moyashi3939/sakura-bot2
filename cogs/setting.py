import logging
import time

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool
from utils.perms import admin_or_owner

logger = logging.getLogger(__name__)

# ==========================================
# 1. テーブル作成 & カラム自動追加 用 SQL
# ==========================================
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,

    -- LevelingCog 連携用
    log_levelup_channel_id BIGINT,
    log_rankup_channel_id BIGINT,
    leveling_enabled BOOLEAN DEFAULT TRUE,

    -- QuizCog 連携用
    quiz_answer_time INT DEFAULT 30,
    quiz_time_limit INT DEFAULT 60,
    quiz_channel_id BIGINT,
    quiz_answer_timeout INT,   -- 未設定(NULL)なら QuizCog の既定値(15秒)
    quiz_total_timeout INT,    -- 未設定(NULL)なら QuizCog の既定値(900秒)

    -- ModerationCog 連携用
    reishou_channel_id BIGINT,
    mod_log_channel_id BIGINT,
    auto_mod_enabled BOOLEAN DEFAULT TRUE,

    -- EEWCog 連携用
    eew_channel_id BIGINT,

    -- ReminderCog 連携用
    reminder_channel_id BIGINT,

    -- 機能ON/OFF
    greetings_enabled BOOLEAN DEFAULT TRUE,
    calculator_enabled BOOLEAN DEFAULT TRUE,

    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 既存テーブルが存在する場合に足りない列を自動で追加するマイグレーションSQL
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS log_levelup_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS log_rankup_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS leveling_enabled BOOLEAN DEFAULT TRUE;

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS quiz_answer_time INT DEFAULT 30;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS quiz_time_limit INT DEFAULT 60;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS quiz_channel_id BIGINT;
-- 旧 quiz_answer_time / quiz_time_limit は既定値(30/60)が入っていて「設定済み」と区別できず、
-- クイズ側からも参照されていなかったため、NULL許容の新カラムを使う
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS quiz_answer_timeout INT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS quiz_total_timeout INT;

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS reishou_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS mod_log_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS auto_mod_enabled BOOLEAN DEFAULT TRUE;

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS eew_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS reminder_channel_id BIGINT;

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS greetings_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS calculator_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- 旧 dice.py が同じテーブルに `enabled BOOLEAN NOT NULL`（デフォルト無し）を作っていた場合、
-- /setting から新規サーバーを INSERT したときに NOT NULL 違反になるため、デフォルト値を付けておく。
-- （ダイス設定自体は dice_settings テーブルに移行済み）
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'guild_settings' AND column_name = 'enabled'
    ) THEN
        ALTER TABLE guild_settings ALTER COLUMN enabled SET DEFAULT FALSE;
    END IF;
END $$;
"""

# 更新を許可するカラム（SQL文を組み立てるため、ここにあるものだけを使う）
ALLOWED_COLUMNS = {
    "log_levelup_channel_id", "log_rankup_channel_id", "leveling_enabled",
    "quiz_answer_timeout", "quiz_total_timeout", "quiz_channel_id",
    "reishou_channel_id", "mod_log_channel_id", "auto_mod_enabled",
    "eew_channel_id", "reminder_channel_id",
    "greetings_enabled", "calculator_enabled",
}

# /setting clear で解除できる項目: 選択肢の値 → (カラム名, 表示名)
CLEARABLE = {
    "levelup_channel": ("log_levelup_channel_id", "レベルアップ通知先"),
    "rankup_channel": ("log_rankup_channel_id", "ランクアップ通知先"),
    "quiz_channel": ("quiz_channel_id", "クイズ専用チャンネル"),
    "reishou_channel": ("reishou_channel_id", "冷笑削除チャンネル"),
    "mod_log_channel": ("mod_log_channel_id", "モデレーションログ"),
    "eew_channel": ("eew_channel_id", "地震速報送信先"),
    "reminder_channel": ("reminder_channel_id", "リマインダー通知先"),
}

# 機能ON/OFF: 機能名 → カラム名
FEATURE_COLUMNS = {
    "greetings": "greetings_enabled",
    "calculator": "calculator_enabled",
}


# ==========================================
# 2. スタンドアロン型ヘルパー関数
# ==========================================
async def get_guild_settings(pool: asyncpg.Pool, guild_id: int) -> dict | None:
    """指定したサーバーの設定を辞書形式で取得する関数（汎用）"""
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM guild_settings WHERE guild_id = $1::bigint", guild_id
        )
        return dict(row) if row else None


class SettingsCog(commands.Cog):
    """設定管理用 Cog (LevelingCog, QuizCog, ModerationCog, EEWCog, ReminderCog 連携)"""

    # on_message のたびにDBへ問い合わせないための簡易キャッシュ（秒）
    CACHE_TTL = 30.0

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[int, tuple[float, dict | None]] = {}

    @property
    def db(self) -> asyncpg.Pool:
        return getattr(self.bot, "db_pool", None)

    # ==================================================
    # 💾 キャッシュ付き取得 / 更新ヘルパー
    # ==================================================
    def invalidate(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            self._cache.clear()
        else:
            self._cache.pop(guild_id, None)

    async def get_setting(self, guild_id: int) -> dict | None:
        """指定ギルドの全設定を取得（30秒キャッシュ）"""
        now = time.monotonic()
        hit = self._cache.get(guild_id)
        if hit and now - hit[0] < self.CACHE_TTL:
            return dict(hit[1]) if hit[1] is not None else None

        data = await get_guild_settings(self.db, guild_id)
        self._cache[guild_id] = (now, data)
        return dict(data) if data is not None else None

    async def _upsert(self, guild_id: int, **cols) -> None:
        """None 以外の値だけを保存する（未指定の項目は変更しない）"""
        cols = {k: v for k, v in cols.items() if v is not None}
        if not cols:
            return
        if not set(cols) <= ALLOWED_COLUMNS:
            raise ValueError(f"許可されていないカラム: {set(cols) - ALLOWED_COLUMNS}")

        names = list(cols)
        placeholders = ", ".join(f"${i}" for i in range(2, len(names) + 2))
        updates = ", ".join(f"{n} = EXCLUDED.{n}" for n in names)
        sql = (
            f"INSERT INTO guild_settings (guild_id, {', '.join(names)}) "
            f"VALUES ($1::bigint, {placeholders}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates}, updated_at = CURRENT_TIMESTAMP"
        )
        async with self.db.acquire() as conn:
            await conn.execute(sql, guild_id, *cols.values())
        self.invalidate(guild_id)

    async def _db_ready(self, interaction: discord.Interaction) -> bool:
        if self.db is None:
            await interaction.response.send_message(
                "❌ データベースに接続できていないため、設定を変更できません。", ephemeral=True
            )
            return False
        return True

    # ==================================================
    # 🤝 各 Cog 連携用インターフェース (Getter メソッド)
    # ==================================================

    # 1. LevelingCog 連携用
    async def get_leveling_config(self, guild_id: int) -> dict:
        settings = await self.get_setting(guild_id)
        if not settings:
            return {
                "levelup_channel_id": None,
                "rankup_channel_id": None,
                "enabled": True,
            }
        return {
            "levelup_channel_id": settings.get("log_levelup_channel_id"),
            "rankup_channel_id": settings.get("log_rankup_channel_id"),
            "enabled": settings.get("leveling_enabled", True),
        }

    # 2. QuizCog 連携用
    QUIZ_DEFAULT_ANSWER_TIMEOUT = 15
    QUIZ_DEFAULT_TOTAL_TIMEOUT = 900

    async def get_quiz_config(self, guild_id: int) -> dict:
        settings = await self.get_setting(guild_id) or {}
        return {
            "answer_time": settings.get("quiz_answer_timeout") or self.QUIZ_DEFAULT_ANSWER_TIMEOUT,
            "time_limit": settings.get("quiz_total_timeout") or self.QUIZ_DEFAULT_TOTAL_TIMEOUT,
            "quiz_channel_id": settings.get("quiz_channel_id"),
        }

    async def get_quiz_settings(self, guild_id: int) -> dict:
        """QuizCog 用: 秒数を float で返す"""
        c = await self.get_quiz_config(guild_id)
        return {"quiz_timeout": float(c["time_limit"]), "answer_timeout": float(c["answer_time"])}

    # 3. ModerationCog 連携用
    async def get_moderation_config(self, guild_id: int) -> dict:
        settings = await self.get_setting(guild_id)
        if not settings:
            return {
                "reishou_channel_id": None,
                "mod_log_channel_id": None,
                "auto_mod_enabled": True,
            }
        return {
            "reishou_channel_id": settings.get("reishou_channel_id"),
            "mod_log_channel_id": settings.get("mod_log_channel_id"),
            "auto_mod_enabled": settings.get("auto_mod_enabled", True),
        }

    # 4. EEWCog 連携用
    async def get_all_eew_targets(self) -> list[int]:
        """地震速報送信対象のチャンネルIDリストを取得"""
        if not self.db:
            return []
        try:
            async with self.db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT eew_channel_id FROM guild_settings WHERE eew_channel_id IS NOT NULL"
                )
                return [row["eew_channel_id"] for row in rows]
        except Exception as e:
            logger.error(f"[SettingsCog] EEW送信先の取得失敗: {e}")
            return []

    # 5. ReminderCog 連携用
    async def get_reminder_channel_id(self, guild_id: int) -> int | None:
        """指定ギルドのリマインダー送信先チャンネルIDを取得"""
        settings = await self.get_setting(guild_id)
        if not settings:
            return None
        return settings.get("reminder_channel_id")

    # 6. 機能ON/OFF（挨拶・電卓など）
    async def is_feature_enabled(self, guild_id: int, feature: str) -> bool:
        """未設定のサーバーは有効扱い。DBに接続できない場合も有効扱い。"""
        column = FEATURE_COLUMNS[feature]
        try:
            settings = await self.get_setting(guild_id)
        except Exception as e:
            logger.warning(f"[SettingsCog] 機能設定の取得に失敗: {e}")
            return True
        if not settings:
            return True
        return settings.get(column) is not False

    # ==================================================
    # 🎛️ Slash Commands (/setting ...)
    # ==================================================
    setting_group = app_commands.Group(
        name="setting", description="Botの各種設定を行います（サーバー管理権限が必要）", guild_only=True
    )

    # --------------------------------------------------
    # /setting leveling (LevelingCog 用)
    # --------------------------------------------------
    @setting_group.command(
        name="leveling", description="レベル・ランクアップ通知や有効化を設定します"
    )
    @app_commands.describe(
        levelup_channel="レベルアップ通知先のチャンネル",
        rankup_channel="ランクアップ通知先のチャンネル",
        enabled="レベル機能を有効にするか",
    )
    @admin_or_owner(manage_guild=True)
    async def setting_leveling(
        self,
        interaction: discord.Interaction,
        levelup_channel: discord.TextChannel | None = None,
        rankup_channel: discord.TextChannel | None = None,
        enabled: bool | None = None,
    ):
        if levelup_channel is None and rankup_channel is None and enabled is None:
            await interaction.response.send_message(
                "設定変更する項目を少なくとも1つ指定してください。", ephemeral=True
            )
            return
        if not await self._db_ready(interaction):
            return

        await self._upsert(
            interaction.guild_id,
            log_levelup_channel_id=levelup_channel.id if levelup_channel else None,
            log_rankup_channel_id=rankup_channel.id if rankup_channel else None,
            leveling_enabled=enabled,
        )

        msg = "レベル機能の設定を更新しました:\n"
        if levelup_channel:
            msg += f"- レベルアップ通知先: {levelup_channel.mention}\n"
        if rankup_channel:
            msg += f"- ランクアップ通知先: {rankup_channel.mention}\n"
        if enabled is not None:
            msg += f"- レベル機能状態: {'有効' if enabled else '無効'}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    # --------------------------------------------------
    # /setting quiz (QuizCog 用)
    # --------------------------------------------------
    @setting_group.command(
        name="quiz", description="クイズの制限時間や専用チャンネルを設定します"
    )
    @app_commands.describe(
        answer_time="早押し後に答えを入力できる時間（秒・既定15）",
        time_limit="クイズ全体の制限時間（秒・既定900）",
        quiz_channel="クイズ実施専用チャンネル",
    )
    @admin_or_owner(manage_guild=True)
    async def setting_quiz(
        self,
        interaction: discord.Interaction,
        answer_time: app_commands.Range[int, 5, 600] | None = None,
        time_limit: app_commands.Range[int, 10, 3600] | None = None,
        quiz_channel: discord.TextChannel | None = None,
    ):
        if answer_time is None and time_limit is None and quiz_channel is None:
            await interaction.response.send_message(
                "設定変更する項目を少なくとも1つ指定してください。", ephemeral=True
            )
            return
        if not await self._db_ready(interaction):
            return

        await self._upsert(
            interaction.guild_id,
            quiz_answer_timeout=answer_time,
            quiz_total_timeout=time_limit,
            quiz_channel_id=quiz_channel.id if quiz_channel else None,
        )

        msg = "クイズの設定を更新しました:\n"
        if answer_time is not None:
            msg += f"- 回答受付時間: {answer_time}秒\n"
        if time_limit is not None:
            msg += f"- 全体制限時間: {time_limit}秒\n"
        if quiz_channel:
            msg += f"- 専用チャンネル: {quiz_channel.mention}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    # --------------------------------------------------
    # /setting moderation (ModerationCog 用)
    # --------------------------------------------------
    @setting_group.command(
        name="moderation", description="モデレーション・冷笑削除・ログチャンネルを設定します"
    )
    @app_commands.describe(
        reishou_channel="冷笑自動削除を適用するチャンネル",
        mod_log_channel="モデレーションログの送信先チャンネル",
        auto_mod="自動モデレーションを有効にするか",
    )
    @admin_or_owner(manage_guild=True)
    async def setting_moderation(
        self,
        interaction: discord.Interaction,
        reishou_channel: discord.TextChannel | None = None,
        mod_log_channel: discord.TextChannel | None = None,
        auto_mod: bool | None = None,
    ):
        if reishou_channel is None and mod_log_channel is None and auto_mod is None:
            await interaction.response.send_message(
                "設定変更する項目を少なくとも1つ指定してください。", ephemeral=True
            )
            return
        if not await self._db_ready(interaction):
            return

        await self._upsert(
            interaction.guild_id,
            reishou_channel_id=reishou_channel.id if reishou_channel else None,
            mod_log_channel_id=mod_log_channel.id if mod_log_channel else None,
            auto_mod_enabled=auto_mod,
        )

        msg = "モデレーション設定を更新しました:\n"
        if reishou_channel:
            msg += f"- 冷笑削除チャンネル: {reishou_channel.mention}\n"
        if mod_log_channel:
            msg += f"- モデレーションログ: {mod_log_channel.mention}\n"
        if auto_mod is not None:
            msg += f"- 自動モデレーション: {'有効' if auto_mod else '無効'}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    # --------------------------------------------------
    # /setting eew (EEWCog 用)
    # --------------------------------------------------
    @setting_group.command(
        name="eew", description="地震速報の送信先チャンネルを設定します"
    )
    @app_commands.describe(channel="送信先のチャンネル")
    @admin_or_owner(manage_guild=True)
    async def setting_eew(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        if not await self._db_ready(interaction):
            return
        await self._upsert(interaction.guild_id, eew_channel_id=channel.id)
        await interaction.response.send_message(
            f"地震速報送信先チャンネルを {channel.mention} に設定しました。", ephemeral=True
        )

    # --------------------------------------------------
    # /setting reminder (ReminderCog 用)
    # --------------------------------------------------
    @setting_group.command(
        name="reminder", description="リマインダーの通知先チャンネルを設定します"
    )
    @app_commands.describe(channel="リマインドを送信するチャンネル")
    @admin_or_owner(manage_guild=True)
    async def setting_reminder(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        if not await self._db_ready(interaction):
            return
        await self._upsert(interaction.guild_id, reminder_channel_id=channel.id)
        await interaction.response.send_message(
            f"✅ リマインダーの通知先チャンネルを {channel.mention} に設定しました。",
            ephemeral=True,
        )

    # --------------------------------------------------
    # /setting features (挨拶・電卓のON/OFF)  ※ダイスは /roll
    # --------------------------------------------------
    @setting_group.command(
        name="features", description="挨拶への自動返信・電卓機能のON/OFFを切り替えます"
    )
    @app_commands.describe(
        greetings="「おはよう」などへの自動返信を有効にするか",
        calculator="数式メッセージの自動計算を有効にするか",
    )
    @admin_or_owner(manage_guild=True)
    async def setting_features(
        self,
        interaction: discord.Interaction,
        greetings: bool | None = None,
        calculator: bool | None = None,
    ):
        if greetings is None and calculator is None:
            await interaction.response.send_message(
                "設定変更する項目を少なくとも1つ指定してください。", ephemeral=True
            )
            return
        if not await self._db_ready(interaction):
            return

        await self._upsert(
            interaction.guild_id,
            greetings_enabled=greetings,
            calculator_enabled=calculator,
        )

        msg = "機能設定を更新しました:\n"
        if greetings is not None:
            msg += f"- 挨拶への自動返信: {'有効' if greetings else '無効'}\n"
        if calculator is not None:
            msg += f"- 自動計算: {'有効' if calculator else '無効'}\n"
        await interaction.response.send_message(msg, ephemeral=True)

    # --------------------------------------------------
    # /setting clear (設定済みチャンネルの解除)
    # --------------------------------------------------
    @setting_group.command(
        name="clear", description="設定済みのチャンネル設定を解除（未設定に戻す）します"
    )
    @app_commands.describe(target="解除する項目")
    @app_commands.choices(
        target=[app_commands.Choice(name=label, value=key) for key, (_, label) in CLEARABLE.items()]
    )
    @admin_or_owner(manage_guild=True)
    async def setting_clear(self, interaction: discord.Interaction, target: str):
        if target not in CLEARABLE:
            await interaction.response.send_message("❌ 不明な項目です。", ephemeral=True)
            return
        if not await self._db_ready(interaction):
            return

        column, label = CLEARABLE[target]
        async with self.db.acquire() as conn:
            await conn.execute(
                f"UPDATE guild_settings SET {column} = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE guild_id = $1::bigint",
                interaction.guild_id,
            )
        self.invalidate(interaction.guild_id)
        await interaction.response.send_message(
            f"🧹 **{label}** の設定を解除しました。", ephemeral=True
        )

    # --------------------------------------------------
    # /setting status (確認コマンド)
    # --------------------------------------------------
    @setting_group.command(
        name="status", description="現在のすべての設定状況を確認します"
    )
    @admin_or_owner(manage_guild=True)
    async def setting_status(self, interaction: discord.Interaction):
        if not await self._db_ready(interaction):
            return
        self.invalidate(interaction.guild_id)  # 常に最新を表示
        settings = await self.get_setting(interaction.guild_id)

        if not settings:
            embed = discord.Embed(
                title="⚙️ 設定状況",
                description="まだ設定が登録されていません。",
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(title="⚙️ 現在の設定状況", color=discord.Color.blue())

        def ch_fmt(ch_id):
            return f"<#{ch_id}>" if ch_id else "未設定"

        def onoff(value, default=True):
            return "有効" if settings.get(value, default) is not False else "無効"

        embed.add_field(
            name="📊 レベル機能",
            value=(
                f"状態: {onoff('leveling_enabled')}\n"
                f"LevelUp: {ch_fmt(settings.get('log_levelup_channel_id'))}\n"
                f"RankUp: {ch_fmt(settings.get('log_rankup_channel_id'))}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧩 クイズ設定",
            value=(
                f"専用Ch: {ch_fmt(settings.get('quiz_channel_id'))}\n"
                f"回答時間: {settings.get('quiz_answer_timeout') or self.QUIZ_DEFAULT_ANSWER_TIMEOUT}秒\n"
                f"制限時間: {settings.get('quiz_total_timeout') or self.QUIZ_DEFAULT_TOTAL_TIMEOUT}秒"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️ モデレーション",
            value=(
                f"自動Mod: {onoff('auto_mod_enabled')}\n"
                f"冷笑削除Ch: {ch_fmt(settings.get('reishou_channel_id'))}\n"
                f"ModログCh: {ch_fmt(settings.get('mod_log_channel_id'))}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🚨 地震速報",
            value=f"送信先Ch: {ch_fmt(settings.get('eew_channel_id'))}",
            inline=False,
        )
        embed.add_field(
            name="⏰ リマインダー",
            value=f"送信先Ch: {ch_fmt(settings.get('reminder_channel_id'))}",
            inline=False,
        )
        embed.add_field(
            name="🔧 機能ON/OFF",
            value=(
                f"挨拶への自動返信: {onoff('greetings_enabled')}\n"
                f"自動計算: {onoff('calculator_enabled')}\n"
                "ダイス: `/roll` で切り替え"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    pool = await get_pool(bot)
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)

    await bot.add_cog(SettingsCog(bot))
