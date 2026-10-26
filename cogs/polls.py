"""投票と抽選（Giveaway）。抽選は再起動後も自動で締め切られる（バックグラウンドループ）。"""

import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import helpers as h

MAX_GIVEAWAY_SECONDS = 30 * 86400
GIVEAWAY_KEEP_SECONDS = 30 * 86400  # 終了後、再抽選のために記録を残す期間
NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


class GiveawayView(discord.ui.View):
    """参加ボタン付き抽選（永続）。"""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🎉 参加", style=discord.ButtonStyle.success, custom_id="giveaway:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        key = f"giveaway_{interaction.message.id}"
        data = self.bot.store.get_setting(interaction.guild.id, key)
        if not data or data.get("drawn"):
            return await interaction.response.send_message(embed=h.error_embed("終了しています", "この抽選は終了しています。"), ephemeral=True)
        entrants = set(data.get("entrants", []))
        if interaction.user.id in entrants:
            entrants.discard(interaction.user.id)
            msg = "参加を取り消しました。"
        else:
            entrants.add(interaction.user.id)
            msg = "抽選に参加しました！ 🎉"
        data["entrants"] = list(entrants)
        self.bot.store.set_setting(interaction.guild.id, key, data)
        await interaction.response.send_message(embed=h.ok_embed("抽選", f"{msg}\n現在の参加者: {len(entrants)}人（もう一度押すと取り消せます）"), ephemeral=True)


class Polls(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    event = app_commands.Group(name="イベント", description="🎉 投票と抽選（抽選の開催・再抽選・終了はサーバー管理が必要）")

    @event.command(name="投票", description="選択肢つきの投票を作成（最大10択）")
    @app_commands.rename(question="質問", options="選択肢")
    @app_commands.describe(question="投票のタイトル", options="「|」で区切って2〜10個 例: りんご|みかん|ぶどう")
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200], options: app_commands.Range[str, 1, 1500]):
        opts = [o.strip() for o in options.split("|") if o.strip()]
        if len(opts) < 2:
            return await interaction.response.send_message(embed=h.error_embed("選択肢が足りません", "選択肢は2個以上必要です（「|」で区切ります）。例: `りんご|みかん`"), ephemeral=True)
        if len(opts) > 10:
            return await interaction.response.send_message(embed=h.error_embed("選択肢が多すぎます", "選択肢は最大10個までです。"), ephemeral=True)
        if not interaction.channel.permissions_for(interaction.guild.me).add_reactions:
            return await interaction.response.send_message(embed=h.error_embed("投票を作れません", "Bot がこのチャンネルでリアクションを付けられません。権限を確認してください。"), ephemeral=True)
        desc = "\n".join(f"{NUMBER_EMOJIS[i]} {opt}" for i, opt in enumerate(opts))
        e = h.embed(f"📊 {question}", desc, h.INFO)
        e.set_footer(text=f"作成者: {interaction.user.display_name}　番号のリアクションで投票します")
        await interaction.response.send_message(embed=e)
        msg = await interaction.original_response()
        for i in range(len(opts)):
            await msg.add_reaction(NUMBER_EMOJIS[i])

    @event.command(name="抽選", description="ボタンで参加する抽選を開催")
    @app_commands.rename(prize="賞品", duration="期間", winners="当選人数")
    @app_commands.describe(prize="賞品名", duration="開催期間 例: 10m, 1h, 2d（最大30日）", winners="当選者の人数 1〜20（省略時: 1）")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str,
                       winners: app_commands.Range[int, 1, 20] = 1):
        seconds = h.parse_duration(duration)
        if not seconds or seconds > MAX_GIVEAWAY_SECONDS:
            return await interaction.response.send_message(embed=h.error_embed("期間を読み取れません", "例: `10m` `1h` `2d`（最大30日）"), ephemeral=True)
        end_ts = int(time.time()) + seconds
        e = h.embed(f"🎉 抽選: {prize}", f"下のボタンから参加できます（もう一度押すと取り消し）。\n\n当選人数: **{winners}**\n終了: <t:{end_ts}:R>", h.OK)
        e.set_footer(text=f"主催: {interaction.user.display_name}")
        await interaction.response.send_message(embed=e, view=GiveawayView(self.bot))
        msg = await interaction.original_response()
        self.bot.store.set_setting(interaction.guild.id, f"giveaway_{msg.id}", {
            "prize": prize, "winners": winners, "entrants": [],
            "channel_id": interaction.channel.id, "end_ts": end_ts, "drawn": False,
            "host": interaction.user.display_name,
        })

    @tasks.loop(seconds=20)
    async def check_giveaways(self):
        now = time.time()
        for guild_id, key, data in self.bot.store.get_kv_by_prefix("giveaway_"):
            if data.get("drawn"):
                if data.get("end_ts", 0) < now - GIVEAWAY_KEEP_SECONDS:
                    self.bot.store.delete_setting(guild_id, key)  # 古い記録を残し続けて走査コストが増えるのを防ぐ
                continue
            if data.get("end_ts", 0) > now:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild:
                try:
                    message_id = int(key.split("_", 1)[1])
                    await self._draw(guild, message_id, data)
                except Exception as exc:  # 1件の失敗でループ全体が停止しないようにする
                    print(f"抽選の締め切りに失敗: {key}: {exc!r}")

    @check_giveaways.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _draw(self, guild, message_id, data=None):
        key = f"giveaway_{message_id}"
        # 渡されたデータは古いかもしれないため、常に最新を読み直す（定期処理と /イベント 抽選終了 の二重発表を防ぐ）
        data = self.bot.store.get_setting(guild.id, key)
        if not data or data.get("drawn"):
            return
        # 先に「終了」を記録してから発表する（発表の途中で別の処理が同じ抽選を始めないように）
        data["drawn"] = True
        self.bot.store.set_setting(guild.id, key, data)
        channel = guild.get_channel(data["channel_id"])
        entrants = data.get("entrants", [])
        winners = []
        if channel is None:
            print(f"抽選の発表先が見つかりません: {key}（当選者の抽選は行い、記録だけ残します）")
        try:
            if not entrants:
                if channel:
                    await channel.send(embed=h.warn_embed("抽選が終了しました", f"**{data['prize']}** — 参加者がいませんでした。"))
            else:
                n = min(data["winners"], len(entrants))
                winners = random.sample(entrants, n)
                mentions = " ".join(f"<@{w}>" for w in winners)
                if channel:
                    await channel.send(f"🎉 おめでとうございます！ {mentions}", embed=h.embed("抽選結果", f"**{data['prize']}** の当選者:\n{mentions}", h.OK))
        except discord.HTTPException as exc:
            # 送信できなくても drawn を立てないと20秒ごとに再試行し続ける。結果は data に残す
            print(f"抽選結果の送信に失敗: {key}: {exc!r} 当選者={winners}")
        data["winner_ids"] = winners
        # reroll 用に記録は残す
        self.bot.store.set_setting(guild.id, key, data)
        await self._mark_finished(channel, message_id, data, winners)

    @staticmethod
    async def _mark_finished(channel, message_id, data, winners):
        """元の抽選メッセージを「終了」表示に更新し、参加ボタンを外す（失敗しても抽選結果には影響しない）。"""
        if channel is None:
            return
        result = " ".join(f"<@{w}>" for w in winners) if winners else "参加者なし"
        e = h.embed(f"🎉 抽選（終了）: {data['prize']}",
                    f"参加者: **{len(data.get('entrants', []))}人**\n当選: {result}", h.LOG)
        if data.get("host"):
            e.set_footer(text=f"主催: {data['host']}")
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=e, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    def _giveaway_choices(self, guild_id: int, current: str, finished: bool):
        out = []
        for gid, key, data in self.bot.store.get_kv_by_prefix("giveaway_"):
            if gid != guild_id or bool(data.get("drawn")) != finished:
                continue
            mid = key.split("_", 1)[1]
            label = f"{data.get('prize', '?')[:60]}（{mid}）"
            if current in mid or current.lower() in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=mid))
        return out[:25]

    async def _active_giveaways(self, interaction: discord.Interaction, current: str):
        return self._giveaway_choices(interaction.guild.id, current, finished=False) if interaction.permissions.manage_guild else []

    async def _finished_giveaways(self, interaction: discord.Interaction, current: str):
        return self._giveaway_choices(interaction.guild.id, current, finished=True) if interaction.permissions.manage_guild else []

    @event.command(name="抽選終了", description="開催中の抽選を今すぐ終了して当選者を決定")
    @app_commands.rename(message_id="メッセージid")
    @app_commands.describe(message_id="終了する抽選（候補から選べます）")
    @app_commands.autocomplete(message_id=_active_giveaways)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end_giveaway(self, interaction: discord.Interaction, message_id: str):
        if not h.is_id(message_id):
            return await interaction.response.send_message(embed=h.error_embed("入力エラー", "メッセージidは数字だけで入力してください。"), ephemeral=True)
        mid = int(message_id)
        data = self.bot.store.get_setting(interaction.guild.id, f"giveaway_{mid}")
        if not data:
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", "そのIDの抽選が見つかりません。"), ephemeral=True)
        if data.get("drawn"):
            return await interaction.response.send_message(embed=h.warn_embed("すでに終了しています", "やり直す場合は `/イベント 再抽選` を使います。"), ephemeral=True)
        await interaction.response.send_message(embed=h.ok_embed("終了します", "当選者を決めて発表します。"), ephemeral=True)
        await self._draw(interaction.guild, mid, data)

    @event.command(name="再抽選", description="終了した抽選をやり直し")
    @app_commands.rename(message_id="メッセージid")
    @app_commands.describe(message_id="やり直す抽選（候補から選べます）")
    @app_commands.autocomplete(message_id=_finished_giveaways)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, message_id: str):
        if not h.is_id(message_id):
            return await interaction.response.send_message(embed=h.error_embed("入力エラー", "メッセージidは数字だけで入力してください。"), ephemeral=True)
        mid = int(message_id)
        key = f"giveaway_{mid}"
        data = self.bot.store.get_setting(interaction.guild.id, key)
        if not data or not data.get("entrants"):
            return await interaction.response.send_message(embed=h.error_embed("やり直せません", "対象の抽選が見つからないか、参加者がいません。"), ephemeral=True)
        if not data.get("drawn"):
            return await interaction.response.send_message(embed=h.error_embed("やり直せません", "この抽選はまだ終了していません。終了後に実行するか、`/イベント 抽選終了` で今すぐ終了してください。"), ephemeral=True)
        # 前回の当選者を除いて、当選人数ぶん選び直す（候補が足りなければ全員から選ぶ）
        previous = set(data.get("winner_ids", []))
        pool = [u for u in data["entrants"] if u not in previous] or data["entrants"]
        winners = random.sample(pool, min(data["winners"], len(pool)))
        data["winner_ids"] = winners
        self.bot.store.set_setting(interaction.guild.id, key, data)
        mentions = " ".join(f"<@{w}>" for w in winners)
        await interaction.response.send_message(embed=h.embed("🎉 再抽選", f"**{data['prize']}** の新しい当選者: {mentions}", h.OK))


async def setup(bot):
    cog = Polls(bot)
    await bot.add_cog(cog)
    bot.add_view(GiveawayView(bot))
