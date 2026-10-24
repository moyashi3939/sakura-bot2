"""サーバー管理者向けモデレーションツール (/mod ...) と情報コマンド。

・すべての /mod コマンドは「該当する権限を持つメンバー」または「ボットオーナー」が実行できます。
  （ボットオーナーはDiscord上の権限・ロール順位に関係なく実行可能。ただしBot自身のロール順位より
    上の相手や、サーバーオーナーには Discord の仕様上、Botでも操作できません）
・実行内容は /setting moderation で設定した「モデレーションログ」チャンネルに記録されます。
"""
import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.db import get_pool
from utils.perms import admin_or_owner

logger = logging.getLogger(__name__)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mod_warnings (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    moderator_id BIGINT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mod_warnings_guild_user ON mod_warnings (guild_id, user_id);
"""

NO_MENTIONS = discord.AllowedMentions.none()


class ModToolsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pool = None

    async def cog_load(self):
        self.pool = await get_pool(self.bot)
        async with self.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

    # ==================================================
    # 🔧 共通ヘルパー
    # ==================================================
    async def _check_target(
        self, interaction: discord.Interaction, target: discord.abc.User, *, action: str
    ) -> str | None:
        """操作対象として不適切なら理由を返す（問題なければ None）"""
        guild = interaction.guild
        if target.id == interaction.user.id:
            return f"自分自身に{action}はできません。"
        if target.id == self.bot.user.id:
            return f"Bot自身に{action}はできません。"
        if target.id == guild.owner_id:
            return f"サーバーオーナーに{action}はできません。"

        member = target if isinstance(target, discord.Member) else guild.get_member(target.id)
        if member is None:
            return None  # サーバーにいない相手（BANなど）

        # Bot自身のロール順位より上の相手はDiscord側が拒否する
        if member.top_role >= guild.me.top_role:
            return f"{member.mention} のロールがBotのロール以上のため{action}できません。（Botのロールを上に移動してください）"

        # 実行者のロール順位チェック（ボットオーナー / サーバーオーナーは免除）
        invoker = interaction.user
        if not await self.bot.is_owner(invoker) and invoker.id != guild.owner_id:
            if isinstance(invoker, discord.Member) and member.top_role >= invoker.top_role:
                return f"あなたと同じかそれ以上のロールを持つ相手には{action}できません。"
        return None

    def _bot_lacks(self, interaction: discord.Interaction, **perms: bool) -> str | None:
        have = interaction.guild.me.guild_permissions
        missing = [name for name in perms if not getattr(have, name)]
        if missing:
            from utils.perms import format_permission_names
            return f"Botに {format_permission_names(missing)} の権限がありません。"
        return None

    async def _send_modlog(self, guild: discord.Guild, embed: discord.Embed):
        settings_cog = self.bot.get_cog("SettingsCog")
        if not settings_cog:
            return
        try:
            config = await settings_cog.get_moderation_config(guild.id)
        except Exception:
            return
        channel_id = config.get("mod_log_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel) and channel.permissions_for(guild.me).send_messages:
            try:
                await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    def _log_embed(self, title: str, color: discord.Color, interaction: discord.Interaction,
                   target=None, reason: str | None = None, **fields) -> discord.Embed:
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        if target is not None:
            embed.add_field(name="対象", value=f"{target.mention} (`{target.id}`)", inline=True)
        embed.add_field(name="実行者", value=f"{interaction.user.mention}", inline=True)
        for name, value in fields.items():
            embed.add_field(name=name, value=str(value), inline=True)
        embed.add_field(name="理由", value=(reason or "未指定")[:1000], inline=False)
        return embed

    async def _dm(self, user: discord.abc.User, text: str):
        try:
            await user.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @staticmethod
    def _audit_reason(interaction: discord.Interaction, reason: str | None) -> str:
        return f"{interaction.user} ({interaction.user.id}): {reason or '理由なし'}"[:500]

    # ==================================================
    # 🛡️ /mod コマンド群
    # ==================================================
    mod = app_commands.Group(name="mod", description="モデレーション操作（管理者向け）", guild_only=True)

    # ---- kick ----
    @mod.command(name="kick", description="メンバーをサーバーからキックします")
    @app_commands.describe(member="キックするメンバー", reason="理由")
    @admin_or_owner(kick_members=True)
    async def mod_kick(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        if (err := self._bot_lacks(interaction, kick_members=True)) or (
            err := await self._check_target(interaction, member, action="キック")
        ):
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._dm(member, f"**{interaction.guild.name}** からキックされました。\n理由: {reason or '未指定'}")
        try:
            await member.kick(reason=self._audit_reason(interaction, reason))
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ キックに失敗しました: {e}")
            return
        await interaction.followup.send(f"👢 **{member}** をキックしました。")
        await self._send_modlog(interaction.guild, self._log_embed("👢 キック", discord.Color.orange(), interaction, member, reason))

    # ---- ban ----
    @mod.command(name="ban", description="ユーザーをBANします（サーバーにいない人もIDで指定可）")
    @app_commands.describe(user="BANするユーザー", reason="理由", delete_days="さかのぼってメッセージを削除する日数(0〜7)")
    @admin_or_owner(ban_members=True)
    async def mod_ban(
        self, interaction: discord.Interaction, user: discord.User,
        reason: str | None = None, delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        if (err := self._bot_lacks(interaction, ban_members=True)) or (
            err := await self._check_target(interaction, user, action="BAN")
        ):
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(user.id)
        if member:
            await self._dm(member, f"**{interaction.guild.name}** からBANされました。\n理由: {reason or '未指定'}")
        try:
            await interaction.guild.ban(
                user, reason=self._audit_reason(interaction, reason), delete_message_seconds=delete_days * 86400
            )
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ BANに失敗しました: {e}")
            return
        await interaction.followup.send(f"🔨 **{user}** をBANしました。")
        await self._send_modlog(interaction.guild, self._log_embed("🔨 BAN", discord.Color.red(), interaction, user, reason))

    # ---- unban ----
    @mod.command(name="unban", description="BANを解除します")
    @app_commands.describe(user_id="BAN解除するユーザーのID", reason="理由")
    @admin_or_owner(ban_members=True)
    async def mod_unban(self, interaction: discord.Interaction, user_id: str, reason: str | None = None):
        if err := self._bot_lacks(interaction, ban_members=True):
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        if not user_id.strip().isdigit():
            await interaction.response.send_message("❌ ユーザーIDは数字で指定してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=self._audit_reason(interaction, reason))
        except discord.NotFound:
            await interaction.followup.send("❌ そのユーザーはBANされていません（またはIDが不正です）。")
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ BAN解除に失敗しました: {e}")
            return
        await interaction.followup.send(f"✅ **{user}** のBANを解除しました。")
        await self._send_modlog(interaction.guild, self._log_embed("✅ BAN解除", discord.Color.green(), interaction, user, reason))

    # ---- timeout ----
    @mod.command(name="timeout", description="メンバーをタイムアウト（発言禁止）にします")
    @app_commands.describe(member="対象メンバー", minutes="分数（最大 40320 = 28日）", reason="理由")
    @admin_or_owner(moderate_members=True)
    async def mod_timeout(
        self, interaction: discord.Interaction, member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320], reason: str | None = None,
    ):
        if (err := self._bot_lacks(interaction, moderate_members=True)) or (
            err := await self._check_target(interaction, member, action="タイムアウト")
        ):
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await member.timeout(datetime.timedelta(minutes=minutes), reason=self._audit_reason(interaction, reason))
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ タイムアウトに失敗しました: {e}")
            return
        until = discord.utils.format_dt(discord.utils.utcnow() + datetime.timedelta(minutes=minutes), "f")
        await interaction.followup.send(f"🔇 {member.mention} を {minutes}分間タイムアウトしました。（解除: {until}）", allowed_mentions=NO_MENTIONS)
        await self._send_modlog(
            interaction.guild,
            self._log_embed("🔇 タイムアウト", discord.Color.orange(), interaction, member, reason, 期間=f"{minutes}分"),
        )

    @mod.command(name="untimeout", description="タイムアウトを解除します")
    @app_commands.describe(member="対象メンバー")
    @admin_or_owner(moderate_members=True)
    async def mod_untimeout(self, interaction: discord.Interaction, member: discord.Member):
        if err := self._bot_lacks(interaction, moderate_members=True):
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        try:
            await member.timeout(None, reason=self._audit_reason(interaction, "タイムアウト解除"))
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ 解除に失敗しました: {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"🔈 {member.mention} のタイムアウトを解除しました。", ephemeral=True, allowed_mentions=NO_MENTIONS)
        await self._send_modlog(interaction.guild, self._log_embed("🔈 タイムアウト解除", discord.Color.green(), interaction, member))

    # ---- warn ----
    @mod.command(name="warn", description="メンバーに警告を記録します")
    @app_commands.describe(member="対象メンバー", reason="警告の理由")
    @admin_or_owner(moderate_members=True)
    async def mod_warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if member.bot:
            await interaction.response.send_message("❌ Botに警告は記録できません。", ephemeral=True)
            return
        if err := await self._check_target(interaction, member, action="警告"):
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        reason = reason[:500]
        async with self.pool.acquire() as conn:
            warn_id = await conn.fetchval(
                "INSERT INTO mod_warnings (guild_id, user_id, moderator_id, reason) VALUES ($1, $2, $3, $4) RETURNING id",
                interaction.guild_id, member.id, interaction.user.id, reason,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM mod_warnings WHERE guild_id = $1 AND user_id = $2",
                interaction.guild_id, member.id,
            )
        await self._dm(member, f"**{interaction.guild.name}** で警告を受けました。（累計 {total} 回）\n理由: {reason}")
        await interaction.response.send_message(
            f"⚠️ {member.mention} に警告を記録しました。（警告ID: `{warn_id}` / 累計 {total} 回）",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )
        await self._send_modlog(
            interaction.guild,
            self._log_embed("⚠️ 警告", discord.Color.gold(), interaction, member, reason, 警告ID=warn_id, 累計=f"{total}回"),
        )

    @mod.command(name="warnings", description="メンバーの警告履歴を表示します")
    @app_commands.describe(member="対象メンバー")
    @admin_or_owner(moderate_members=True)
    async def mod_warnings(self, interaction: discord.Interaction, member: discord.Member):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, moderator_id, reason, created_at FROM mod_warnings "
                "WHERE guild_id = $1 AND user_id = $2 ORDER BY id DESC LIMIT 15",
                interaction.guild_id, member.id,
            )
        if not rows:
            await interaction.response.send_message(f"✅ {member.mention} の警告履歴はありません。", ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        embed = discord.Embed(title=f"⚠️ {member.display_name} の警告履歴", color=discord.Color.gold())
        for r in rows:
            embed.add_field(
                name=f"ID {r['id']} - {discord.utils.format_dt(r['created_at'], 'd')}",
                value=f"{r['reason']}\n実行者: <@{r['moderator_id']}>",
                inline=False,
            )
        embed.set_footer(text="最新15件まで表示 / 削除: /mod delwarn")
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @mod.command(name="delwarn", description="警告IDを指定して警告を削除します")
    @app_commands.describe(warn_id="警告ID（/mod warnings で確認）")
    @admin_or_owner(moderate_members=True)
    async def mod_delwarn(self, interaction: discord.Interaction, warn_id: int):
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM mod_warnings WHERE id = $1 AND guild_id = $2", warn_id, interaction.guild_id
            )
        if result == "DELETE 1":
            await interaction.response.send_message(f"🗑️ 警告ID `{warn_id}` を削除しました。", ephemeral=True)
            await self._send_modlog(interaction.guild, self._log_embed("🗑️ 警告削除", discord.Color.light_grey(), interaction, 警告ID=warn_id))
        else:
            await interaction.response.send_message("❌ その警告IDは見つかりません。", ephemeral=True)

    # ---- purge ----
    @mod.command(name="purge", description="このチャンネルの直近メッセージをまとめて削除します")
    @app_commands.describe(amount="確認するメッセージ件数（1〜200）", member="指定するとそのメンバーのメッセージだけ削除")
    @admin_or_owner(manage_messages=True)
    async def mod_purge(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200],
        member: discord.Member | None = None,
    ):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await interaction.response.send_message("❌ このチャンネルでは使用できません。", ephemeral=True)
            return
        if not channel.permissions_for(interaction.guild.me).manage_messages:
            await interaction.response.send_message("❌ Botにこのチャンネルの「メッセージの管理」権限がありません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == member.id) if member else None
        deleted = await channel.purge(limit=amount, check=check, reason=self._audit_reason(interaction, "purge"))
        await interaction.followup.send(f"🧹 {len(deleted)} 件のメッセージを削除しました。")
        await self._send_modlog(
            interaction.guild,
            self._log_embed("🧹 メッセージ一括削除", discord.Color.blurple(), interaction, member, None,
                            チャンネル=channel.mention, 件数=len(deleted)),
        )

    # ---- slowmode ----
    @mod.command(name="slowmode", description="チャンネルの低速モードを設定します（0で解除）")
    @app_commands.describe(seconds="秒数（0〜21600）", channel="対象チャンネル（省略で現在のチャンネル）")
    @admin_or_owner(manage_channels=True)
    async def mod_slowmode(
        self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
    ):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ テキストチャンネルで使用してください。", ephemeral=True)
            return
        if not channel.permissions_for(interaction.guild.me).manage_channels:
            await interaction.response.send_message("❌ Botにそのチャンネルの「チャンネルの管理」権限がありません。", ephemeral=True)
            return
        await channel.edit(slowmode_delay=seconds, reason=self._audit_reason(interaction, "slowmode"))
        text = f"🐢 {channel.mention} の低速モードを **{seconds}秒** に設定しました。" if seconds else f"🐇 {channel.mention} の低速モードを解除しました。"
        await interaction.response.send_message(text, ephemeral=True)
        await self._send_modlog(interaction.guild, self._log_embed("🐢 低速モード変更", discord.Color.blurple(), interaction, None, None, チャンネル=channel.mention, 秒数=seconds))

    # ---- lock / unlock ----
    async def _set_lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None, locked: bool):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ テキストチャンネルで使用してください。", ephemeral=True)
            return
        if not channel.permissions_for(interaction.guild.me).manage_roles:
            await interaction.response.send_message("❌ Botにそのチャンネルの「権限の管理」が必要です。", ephemeral=True)
            return
        everyone = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = False if locked else None  # None = 継承（元の状態に戻す）
        await channel.set_permissions(everyone, overwrite=overwrite, reason=self._audit_reason(interaction, "lock" if locked else "unlock"))
        label = "🔒 ロック" if locked else "🔓 ロック解除"
        await interaction.response.send_message(f"{label}: {channel.mention}", ephemeral=True)
        await self._send_modlog(interaction.guild, self._log_embed(label, discord.Color.dark_grey(), interaction, None, None, チャンネル=channel.mention))

    @mod.command(name="lock", description="チャンネルを @everyone の発言不可にします")
    @app_commands.describe(channel="対象チャンネル（省略で現在のチャンネル）")
    @admin_or_owner(manage_channels=True)
    async def mod_lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await self._set_lock(interaction, channel, True)

    @mod.command(name="unlock", description="チャンネルのロックを解除します")
    @app_commands.describe(channel="対象チャンネル（省略で現在のチャンネル）")
    @admin_or_owner(manage_channels=True)
    async def mod_unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await self._set_lock(interaction, channel, False)

    # ==================================================
    # ℹ️ 情報コマンド（誰でも使用可）
    # ==================================================
    @app_commands.command(name="serverinfo", description="このサーバーの情報を表示します")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        humans = sum(1 for m in g.members if not m.bot)
        embed = discord.Embed(title=f"🏠 {g.name}", color=discord.Color.blurple())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="オーナー", value=f"<@{g.owner_id}>", inline=True)
        embed.add_field(name="メンバー", value=f"{g.member_count}人（人間 {humans} / Bot {g.member_count - humans}）", inline=True)
        embed.add_field(name="作成日", value=discord.utils.format_dt(g.created_at, "D"), inline=True)
        embed.add_field(name="チャンネル", value=f"テキスト {len(g.text_channels)} / ボイス {len(g.voice_channels)}", inline=True)
        embed.add_field(name="ロール数", value=str(len(g.roles)), inline=True)
        embed.add_field(name="ブースト", value=f"Lv.{g.premium_tier}（{g.premium_subscription_count}）", inline=True)
        embed.set_footer(text=f"ID: {g.id}")
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="userinfo", description="ユーザーの情報を表示します")
    @app_commands.describe(member="確認したいメンバー（省略で自分）")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        embed = discord.Embed(title=f"👤 {member.display_name}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ユーザー", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="アカウント作成", value=discord.utils.format_dt(member.created_at, "D"), inline=True)
        if member.joined_at:
            embed.add_field(name="サーバー参加", value=discord.utils.format_dt(member.joined_at, "D"), inline=True)
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        text = " ".join(roles[:15]) + (f" 他{len(roles) - 15}個" if len(roles) > 15 else "")
        embed.add_field(name=f"ロール ({len(roles)})", value=text or "なし", inline=False)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModToolsCog(bot))
