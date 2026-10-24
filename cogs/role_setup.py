import os
import json
import discord
from discord import app_commands
from discord.ext import commands

from utils.perms import admin_or_owner

JSON_FILE = "role_panels.json"

def load_data():
    """JSONファイルからデータを読み込む"""
    if not os.path.exists(JSON_FILE):
        return {}
    
    if os.path.getsize(JSON_FILE) == 0:
        return {}

    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print("⚠️ JSONの構文が不正です。空のデータとして初期化します。")
        return {}
    except Exception as e:
        print(f"⚠️ JSONの読み込み中にエラーが発生しました: {e}")
        return {}

def save_data(data):
    """JSONファイルへデータを保存する"""
    try:
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ JSONの保存中にエラーが発生しました: {e}")


# 💡 1. 永続化（再起動対策）に対応したボタンのViewクラス
class DynamicRoleView(discord.ui.View):
    def __init__(self, role_ids: list):
        super().__init__(timeout=None)
        
        # 受け取ったロールIDの数だけ、動的にボタンを生成して追加する
        for r_id in role_ids:
            button = discord.ui.Button(
                label="ロール切り替え", 
                style=discord.ButtonStyle.primary,
                custom_id=f"dyn_role_{r_id}"
            )
            button.callback = self.create_callback(r_id)
            self.add_item(button)

    def create_callback(self, role_id: int):
        """各ボタンが押されたときの処理を生成する（クロージャ）"""
        async def button_callback(interaction: discord.Interaction):
            guild = interaction.guild
            if not guild:
                await interaction.response.send_message("❌ この操作はサーバー内でのみ実行できます。", ephemeral=True)
                return

            role = guild.get_role(role_id) or discord.utils.get(guild.roles, id=role_id)

            if not role:
                await interaction.response.send_message("⚠️ このロールはサーバー内に存在しないか、削除されています。", ephemeral=True)
                return

            member = interaction.user
            try:
                if role in member.roles:
                    await member.remove_roles(role)
                    await interaction.response.send_message(f"✅ ロール **{role.name}** を外しました。", ephemeral=True)
                else:
                    await member.add_roles(role)
                    await interaction.response.send_message(f"✅ ロール **{role.name}** を付与しました！", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("❌ Botの権限が足りません。Botの役職順位を、対象のロールより上に並び替えてください。", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ エラーが発生しました: {e}", ephemeral=True)

        return button_callback


# 💡 2. コマンドと再起動復旧を管理するCog本体
class RolePanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """【超重要】Bot起動時に、保存されているすべてのロールID用のViewを25個ずつに分割してDiscordに再登録する"""
        data = load_data()
        all_role_ids = set()
        
        # 全サーバーのJSONデータから、登録されているロールIDをすべて集める
        for guild_data in data.values():
            for r_id in guild_data.get("role_ids", []):
                all_role_ids.add(r_id)
            
        if all_role_ids:
            role_ids_list = list(all_role_ids)
            chunk_size = 25
            # DiscordのViewの最大ボタン数（25個）制限対策として分割して登録
            for i in range(0, len(role_ids_list), chunk_size):
                chunk = role_ids_list[i:i + chunk_size]
                self.bot.add_view(DynamicRoleView(chunk))
                
            print(f"⚙️ ロールパネルの永続Viewを再登録しました。 (対象ロール総数: {len(role_ids_list)})")

    @app_commands.command(name="role_setup", description="複数選択できるロール付与パネルを作成します（最大6個）")
    @app_commands.describe(
        role1="必須のロール1つ目",
        role2="任意のロール2つ目",
        role3="任意のロール3つ目",
        role4="任意のロール4つ目",
        role5="任意のロール5つ目",
        role6="任意のロール6つ目"
    )
    @app_commands.guild_only()
    @admin_or_owner()
    async def role_panel(
        self, 
        interaction: discord.Interaction, 
        role1: discord.Role,
        role2: discord.Role = None,
        role3: discord.Role = None,
        role4: discord.Role = None,
        role5: discord.Role = None,
        role6: discord.Role = None
    ):
        # 選択されたロールをリストにまとめる（Noneを除外）
        raw_roles = [role1, role2, role3, role4, role5, role6]
        roles = [r for r in raw_roles if r is not None]

        # @everyone・Bot連携ロール・Botより上位のロールはボタンで付与できないため弾く
        me = interaction.guild.me
        invalid = [r for r in roles if r.is_default() or r.managed or r >= me.top_role]
        if invalid:
            names = "、".join(r.mention for r in invalid)
            await interaction.response.send_message(
                f"❌ 次のロールはパネルに追加できません（@everyone / 連携ロール / Botより上位のロール）: {names}",
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        role_ids = [r.id for r in roles]

        # 📂 データをJSONに保存
        data = load_data()
        if guild_id not in data:
            data[guild_id] = {"role_ids": []}
        
        # 新しいロールIDを既存のデータにマージ（重複は除く）
        updated_role_ids = list(set(data[guild_id]["role_ids"] + role_ids))
        data[guild_id]["role_ids"] = updated_role_ids
        save_data(data)

        # 💡 パネル用のView（ボタン群）を構築
        view = DynamicRoleView(role_ids)
        
        # 各ボタンのラベルを「実際のロール名」に動的に書き換える
        for button, role in zip(view.children, roles):
            button.label = f"✨ {role.name}"

        # 📢 新しく作られたボタンのIDも即時有効化する
        self.bot.add_view(view)

        # パネルの見た目（Embed）を作成
        embed = discord.Embed(
            title="🏷️ ロール付与パネル",
            description="下のボタンを押すことで、対応するロール（役職）を自由につけ外しできます。\nもう一度押すと外れます。",
            color=discord.Color.blue()
        )
        for i, role in enumerate(roles, start=1):
            embed.add_field(name=f"選択肢 {i}", value=role.mention, inline=False)

        # パネルを送信
        await interaction.response.send_message(embed=embed, view=view)


# Cogを有効化するための関数
async def setup(bot: commands.Bot):
    await bot.add_cog(RolePanelCog(bot))