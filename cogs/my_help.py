import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button

from utils.perms import is_admin_command

class HelpPaginator(View):
    def __init__(self, embeds: list[discord.Embed]):
        super().__init__(timeout=180)  # 3分でボタンが無効化されます
        self.embeds = embeds
        self.current_page = 0
        self.update_buttons()

    def update_buttons(self):
        # 最初のページなら「前へ」を無効化
        self.prev_button.disabled = self.current_page == 0
        # 最後のページなら「次へ」を無効化
        self.next_button.disabled = self.current_page == len(self.embeds) - 1
        # ページ番号の表示を更新
        self.page_counter.label = f"{self.current_page + 1} / {len(self.embeds)}"

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.blurple)
    async def prev_button(self, interaction: discord.Interaction, button: Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.gray, disabled=True)
    async def page_counter(self, interaction: discord.Interaction, button: Button):
        pass

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.blurple)
    async def next_button(self, interaction: discord.Interaction, button: Button):
        if self.current_page < len(self.embeds) - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(view=self)
        except:
            pass


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Botのコマンド一覧と使い方を表示します")
    async def help_command(self, interaction: discord.Interaction):
        user_cmds_list = []
        admin_cmds_list = []

        # bot.tree から登録されている全スラッシュコマンドを自動取得
        # admin_or_owner が付いているものは管理者コマンドとして分類する
        for cmd in self.bot.tree.get_commands():
            if isinstance(cmd, app_commands.Command):
                line = f"**/{cmd.name}**\n└ {cmd.description or '説明なし'}\n"
                (admin_cmds_list if is_admin_command(cmd) else user_cmds_list).append(f"🔘 {line}" if is_admin_command(cmd) else f"🔹 {line}")

            elif isinstance(cmd, app_commands.Group):
                for sub_cmd in cmd.commands:
                    if not isinstance(sub_cmd, app_commands.Command):
                        continue
                    line = f"**/{cmd.name} {sub_cmd.name}**\n└ {sub_cmd.description or '説明なし'}\n"
                    if is_admin_command(sub_cmd):
                        admin_cmds_list.append(f"🔘 {line}")
                    else:
                        user_cmds_list.append(f"🔹 {line}")

        # 1024文字オーバーを防ぐためのチャンク（分割）関数
        def create_chunks(commands_list):
            chunks = []
            current_chunk = []
            current_length = 0

            for cmd_str in commands_list:
                if current_length + len(cmd_str) + 1 > 900:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = [cmd_str]
                    current_length = len(cmd_str)
                else:
                    current_chunk.append(cmd_str)
                    current_length += len(cmd_str) + 1
            
            if current_chunk:
                chunks.append("\n".join(current_chunk))
            return chunks

        show_admin = interaction.permissions.administrator or await self.bot.is_owner(interaction.user)
        user_chunks = create_chunks(user_cmds_list) if user_cmds_list else ["コマンドはありません"]
        admin_chunks = create_chunks(admin_cmds_list) if (show_admin and admin_cmds_list) else []

        embeds = []
        bot_avatar = self.bot.user.display_avatar.url if self.bot.user else None

        # 一般コマンドのページを作成
        for i, chunk in enumerate(user_chunks):
            embed = discord.Embed(
                title="📚 sakura-bot2 コマンド一覧",
                description=f"このBotで利用可能なコマンドの一覧です。\n`/` から始まるコマンドは全員が使用できます。",
                color=discord.Color.blurple()
            )
            embed.add_field(name="👥 利用可能な一般コマンド", value=chunk, inline=False)
            embed.set_footer(text="SAKURA-BOT System", icon_url=bot_avatar)
            embeds.append(embed)

        # 管理者コマンドがある場合は専用ページを追加
        if show_admin and admin_chunks:
            for i, chunk in enumerate(admin_chunks):
                embed = discord.Embed(
                    title="📚 sakura-bot2 管理者コマンド一覧",
                    description="管理者権限を持つユーザーのみ実行できるコマンドです。",
                    color=discord.Color.orange()
                )
                embed.add_field(name="🔒 管理者専用コマンド", value=chunk, inline=False)
                embed.set_footer(text="SAKURA-BOT System", icon_url=bot_avatar)
                embeds.append(embed)

        if not embeds:
            embed = discord.Embed(
                title="📚 sakura-bot2 コマンド一覧",
                description="表示できるコマンドがありません。",
                color=discord.Color.blurple()
            )
            embed.set_footer(text="SAKURA-BOT System", icon_url=bot_avatar)
            embeds.append(embed)

        view = HelpPaginator(embeds)
        message = await interaction.response.send_message(embed=embeds[0], view=view)
        view.message = message


    @commands.command(name="help", aliases=["skr_help"], extras={"access": "all"})
    async def prefix_help(self, ctx):
        """テキストコマンド（!skr_）の一覧を表示します"""
        is_owner = await self.bot.is_owner(ctx.author)
        is_admin = is_owner or (ctx.guild is not None and ctx.author.guild_permissions.administrator)

        sections = {
            "all": ("👥 誰でも使えるコマンド", []),
            "admin": ("🔒 管理者コマンド（ボットオーナーは権限不問）", []),
            "owner": ("👑 オーナー専用コマンド", []),
        }
        for cmd in sorted(self.bot.commands, key=lambda c: c.name):
            access = cmd.extras.get("access", "all")
            if access == "owner" and not is_owner:
                continue
            if access == "admin" and not is_admin:
                continue
            desc = cmd.help or (cmd.callback.__doc__.strip() if cmd.callback.__doc__ else "説明文が設定されていません。")
            sections[access][1].append(f"⚡ **{ctx.clean_prefix}{cmd.name}** {cmd.signature}\n└ {desc}\n")

        embed = discord.Embed(
            title="📚 sakura-bot2 テキストコマンド一覧",
            description="`/help` ではスラッシュコマンドの一覧を確認できます。",
            color=discord.Color.red() if is_owner else discord.Color.blurple(),
        )
        for title, items in sections.values():
            # Embedのフィールド上限(1024文字)を超えないよう分割
            chunk, length = [], 0
            for item in items:
                if length + len(item) > 950:
                    embed.add_field(name=title, value="\n".join(chunk), inline=False)
                    chunk, length = [], 0
                chunk.append(item)
                length += len(item)
            if chunk:
                embed.add_field(name=title, value="\n".join(chunk), inline=False)

        bot_avatar = self.bot.user.display_avatar.url if self.bot.user else None
        embed.set_footer(text="SAKURA-BOT System", icon_url=bot_avatar)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))