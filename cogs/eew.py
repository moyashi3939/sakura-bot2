import asyncio
import json
import logging
import re
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import websockets

logger = logging.getLogger(__name__)

# 本番用エンドポイント
P2P_WS_URL = "wss://api.p2pquake.net/v2/ws"
# テスト用（サンドボックス）エンドポイント
# P2P_WS_URL = "wss://api-realtime-sandbox.p2pquake.net/v2/ws"

SCALE_MAP = {
    10: "震度1",
    20: "震度2",
    30: "震度3",
    40: "震度4",
    45: "震度5弱",
    50: "震度5強",
    55: "震度6弱",
    60: "震度6強",
    70: "震度7",
}


class EEWCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bg_task: asyncio.Task | None = None
        self.processed_codes: set[str] = set()  # 重複通知防止（上限を超えたら全消去）
        logger.info("[EEWCog] P2P地震情報版 EEWCog が初期化されました。")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.bg_task is None or self.bg_task.done():
            logger.info("[EEWCog] on_ready を検知しました。listen_p2p タスクを起動します。")
            self.bg_task = self.bot.loop.create_task(self.listen_p2p())

    def cog_unload(self):
        if self.bg_task:
            self.bg_task.cancel()

    async def get_all_eew_targets(self) -> list[int]:
        settings_cog = self.bot.get_cog("SettingsCog")
        if not settings_cog:
            logger.warning("[EEWCog] SettingsCog がロードされていません。")
            return []

        if hasattr(settings_cog, "get_all_eew_targets"):
            return await settings_cog.get_all_eew_targets()
        
        logger.warning("[EEWCog] SettingsCog に get_all_eew_targets メソッドが見つかりません。")
        return []

    # ==========================================
    # 📡 P2P地震情報 WebSocket 接続・受信ループ
    # ==========================================
    async def listen_p2p(self):
        logger.info("[EEWCog] listen_p2p ループが開始されました。")
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            ws = None
            try:
                logger.info(f"[EEWCog] {P2P_WS_URL} へ接続を試みます...")
                
                # 接続確立（ハンドシェイク）の瞬間だけを10秒でタイムアウトさせる
                try:
                    async with asyncio.timeout(10):
                        ws = await websockets.connect(
                            P2P_WS_URL, ping_interval=20, ping_timeout=10
                        ).__aenter__()
                except asyncio.TimeoutError:
                    logger.warning("[EEWCog] P2P WebSocket 接続がタイムアウトしました。5秒後に再接続します...")
                    await asyncio.sleep(5)
                    continue

                logger.info("[EEWCog] 🎉 P2P WebSocket 接続成功！サーバーへ購読リクエストを送信します...")
                
                # 接続直後に「コード551（地震情報）と556（緊急地震速報）」を受け取るためのリクエストを送信
                subscribe_msg = json.dumps({"control": ["551", "556"]})
                await ws.send(subscribe_msg)
                logger.info(f"[EEWCog] 送信した購読リクエスト: {subscribe_msg}")

                async for message in ws:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    code = data.get("code")
                    if code not in [551, 556]:
                        continue

                    # テスト配信（test: true）は通知しない
                    if data.get("test"):
                        continue

                    logger.info(f"[P2P受信データ] ──> code: {code}, 内容: {json.dumps(data, ensure_ascii=False)[:200]}...")

                    target_channels = await self.get_all_eew_targets()
                    if not target_channels:
                        logger.warning("[EEWCog] 通知先チャンネルが設定されていません！")
                        continue

                    embed = None

                    # 1. 緊急地震速報（code: 556）の場合
                    if code == 556:
                        issue = data.get("issue", {})
                        cancelled = issue.get("type") == "Cancel"
                        if cancelled:
                            continue
                        if len(self.processed_codes) > 200:
                            self.processed_codes.clear()

                        # 同じ速報の続報(同一ID)で何度も通知しない
                        event_key = f"556:{data.get('id', data.get('time', ''))}"
                        if event_key in self.processed_codes:
                            continue
                        self.processed_codes.add(event_key)

                        parse_time = data.get("time", "不明")
                        embed = discord.Embed(
                            title="🚨 **【緊急地震速報（警報）】**",
                            description="緊急地震速報が発表されました。",
                            color=discord.Color.red(),
                        )
                        embed.add_field(name="発表日時", value=parse_time, inline=False)
                        embed.set_footer(text="データ提供: P2P地震情報 API")

                    # 2. 地震情報・震度速報（code: 551）の場合
                    elif code == 551:
                        earthquake = data.get("earthquake", {})
                        hypocenter = earthquake.get("hypocenter", {})
                        
                        place = hypocenter.get("name", "不明")
                        time_str = earthquake.get("time", "不明")
                        max_scale = earthquake.get("maxScale", -1)
                        shindo_str = SCALE_MAP.get(max_scale, "不明")
                        mag = hypocenter.get("magnitude", -1)
                        mag_str = f"M{mag}" if mag != -1 else "不明"

                        # ⚠️ テスト時はすべての震度を通すため条件を緩めに（本番運用時は max_scale < 40 にしてください）
                        if max_scale < 40:
                            continue

                        # 重複防止
                        event_id = earthquake.get("time", "") + place
                        if event_id in self.processed_codes:
                            continue
                        self.processed_codes.add(event_id)
                        if len(self.processed_codes) > 200:
                            self.processed_codes.clear()

                        embed = discord.Embed(
                            title=f"⚠️ **【地震情報】最大震度 {shindo_str}**",
                            description=f"**{place}** 付近で地震が発生しました。",
                            color=discord.Color.gold(),
                        )
                        embed.add_field(name="最大震度", value=shindo_str, inline=True)
                        embed.add_field(name="マグニチュード", value=mag_str, inline=True)
                        embed.add_field(name="震源地", value=place, inline=False)
                        embed.set_footer(text=f"発生日時: {time_str} | データ提供: P2P地震情報 API")

                    if embed:
                        for channel_id in target_channels:
                            ch_id = int(channel_id)
                            channel = self.bot.get_channel(ch_id)
                            if not channel:
                                try:
                                    channel = await self.bot.fetch_channel(ch_id)
                                except:
                                    continue
                            if channel and isinstance(channel, discord.TextChannel):
                                try:
                                    await channel.send(embed=embed)
                                    logger.info(f"[EEWCog] チャンネル {ch_id} に地震情報を送信しました。")
                                except Exception as e:
                                    logger.error(f"[EEWCog] 送信エラー ({ch_id}): {e}")

            except asyncio.CancelledError:
                logger.info("[EEWCog] P2P WebSocket タスクが終了しました")
                if ws:
                    await ws.close()
                break
            except Exception as e:
                logger.error(f"[EEWCog] P2P WebSocketエラー: {e}. 5秒後に再接続します...")
                if ws:
                    try:
                        await ws.close()
                    except:
                        pass
                await asyncio.sleep(5)

    # ==========================================
    # 📜 /eew_history コマンド
    # ==========================================
    @app_commands.command(
        name="eew_history",
        description="直近の最大震度4以上の地震履歴（10件）を取得します",
    )
    @app_commands.guild_only()
    async def eew_history(self, interaction: discord.Interaction):
        await interaction.response.defer()

        url = "https://api.p2pquake.net/v2/history?codes=551&limit=100"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers={"User-Agent": "DiscordBot/1.0"}
                ) as resp:
                    if resp.status != 200:
                        await interaction.followup.send("❌ 地震情報の取得に失敗しました。")
                        return

                    data = await resp.json()

            filtered_earthquakes = []
            for item in data:
                earthquake = item.get("earthquake", {})
                max_scale_num = earthquake.get("maxScale", 0)

                if max_scale_num >= 40:
                    filtered_earthquakes.append(item)
                    if len(filtered_earthquakes) >= 10:
                        break

            if not filtered_earthquakes:
                await interaction.followup.send("ℹ️ 直近で観測された震度4以上の地震はありません。")
                return

            embed = discord.Embed(
                title="🌋 直近の地震履歴（最大震度4以上）",
                description="直近で発生した最大震度4以上の地震情報（最大10件）です。",
                color=discord.Color.red(),
            )

            for item in filtered_earthquakes:
                earthquake = item.get("earthquake", {})
                hypocenter = earthquake.get("hypocenter", {})

                place = hypocenter.get("name", "不明")
                time_str = earthquake.get("time", "不明")
                max_scale_num = earthquake.get("maxScale", -1)
                shindo_str = SCALE_MAP.get(max_scale_num, "不明")
                mag = hypocenter.get("magnitude", -1)
                mag_str = f"M{mag}" if mag != -1 else "不明"

                field_value = (
                    f"📍 **震源地:** {place}\n"
                    f"🕒 **日時:** {time_str}\n"
                    f"📊 **最大震度:** {shindo_str}\n"
                    f"🌐 **マグニチュード:** {mag_str}"
                )

                embed.add_field(name=f"地震情報 - {time_str}", value=field_value, inline=False)

            embed.set_footer(text="データ提供: P2P地震情報 API")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"⚠️ エラーが発生しました: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(EEWCog(bot))