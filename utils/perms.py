"""権限チェック用の共通ユーティリティ。

ボットオーナー（アプリケーションのオーナー / Teamメンバー / 環境変数 OWNER_IDS）は、
Discord上の権限やロールに関係なく、ここで定義したチェックを必ず通過できます。
"""
from __future__ import annotations

from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

__all__ = [
    "is_owner",
    "admin_or_owner",
    "prefix_admin_or_owner",
    "prefix_owner_only",
    "is_admin_command",
    "format_permission_names",
]

# 権限名 → 日本語表示
PERMISSION_LABELS: dict[str, str] = {
    "administrator": "管理者",
    "manage_guild": "サーバー管理",
    "manage_channels": "チャンネル管理",
    "manage_messages": "メッセージ管理",
    "manage_roles": "ロール管理",
    "kick_members": "メンバーをキック",
    "ban_members": "メンバーをBAN",
    "moderate_members": "メンバーをタイムアウト",
}


def format_permission_names(names: list[str] | tuple[str, ...]) -> str:
    return "、".join(f"**{PERMISSION_LABELS.get(n, n)}**" for n in names)


async def is_owner(bot: commands.Bot, user: discord.abc.User) -> bool:
    """ボットオーナーかどうか（MyBot.is_owner を呼ぶだけの薄いラッパー）"""
    return await bot.is_owner(user)


def _missing(perms: dict[str, bool], have: discord.Permissions) -> list[str]:
    # administrator を持っていれば他の権限は不問（Discord の仕様と同じ）
    if have.administrator:
        return []
    return [name for name, value in perms.items() if getattr(have, name) != value]


# ------------------------------------------------------------
# スラッシュコマンド用
# ------------------------------------------------------------
def admin_or_owner(**perms: bool) -> Callable:
    """ボットオーナー、または指定した権限を持つサーバーメンバーのみ実行可能。

    使い方:
        @app_commands.command(...)
        @admin_or_owner()                    # 既定は administrator
        @admin_or_owner(manage_guild=True)   # 権限を指定
    """
    required = perms or {"administrator": True}

    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        missing = _missing(required, interaction.permissions)
        if missing:
            raise app_commands.MissingPermissions(missing)
        return True

    # /help での分類や、デバッグ用に目印を付けておく
    predicate.is_admin_check = True  # type: ignore[attr-defined]
    predicate.required_permissions = required  # type: ignore[attr-defined]
    return app_commands.check(predicate)


def is_admin_command(cmd: app_commands.Command) -> bool:
    """admin_or_owner が付いているコマンドか（/help の分類用）"""
    return any(getattr(c, "is_admin_check", False) for c in cmd.checks)


# ------------------------------------------------------------
# プレフィックスコマンド（!skr_xxx）用
# ------------------------------------------------------------
def prefix_admin_or_owner(**perms: bool) -> Callable:
    required = perms or {"administrator": True}

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        # チャンネル単位の上書きも考慮した権限で判定する
        have = ctx.channel.permissions_for(ctx.author)
        missing = _missing(required, have)
        if missing:
            raise commands.MissingPermissions(missing)
        return True

    predicate.is_admin_check = True  # type: ignore[attr-defined]
    return commands.check(predicate)


def prefix_owner_only() -> Callable:
    """ボットオーナー専用（commands.is_owner と同じ挙動だが MyBot.is_owner を経由）"""

    async def predicate(ctx: commands.Context) -> bool:
        if not await ctx.bot.is_owner(ctx.author):
            raise commands.NotOwner("ボットオーナー専用コマンドです。")
        return True

    predicate.is_owner_check = True  # type: ignore[attr-defined]
    return commands.check(predicate)
