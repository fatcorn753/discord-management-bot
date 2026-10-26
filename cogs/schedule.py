"""
予約投稿: 指定した日時にチャンネルへメッセージ（またはお知らせ埋め込み）を自動投稿する。

/予約投稿 追加 : 日時・チャンネル・本文を指定して予約
/予約投稿 一覧 : 未投稿の予約を表示
/予約投稿 取消 : 予約IDを指定して取り消し

予約は SQLite の scheduled テーブルに保存されるため、Bot を再起動しても残る。
Bot 停止中に投稿時刻を過ぎた予約は、遅れが LATE_LIMIT 以内なら起動後に投稿し、
それを超えていれば投稿せずモデログに通知する（古い告知を突然流さないため）。

config.json の utc_offset_hours（既定 9 = 日本時間）が、日時入力の解釈に使われる。
"""

import datetime
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import helpers as h

ACTION = "post"
MAX_PENDING_PER_GUILD = 25
MIN_LEAD = datetime.timedelta(seconds=30)
MAX_LEAD = datetime.timedelta(days=365)
LATE_LIMIT = 3600  # 秒。これ以上遅れた予約は投稿しない
MAX_SEND_ATTEMPTS = 3  # 一時的な Discord 側エラー(5xx)での再試行回数


class Schedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._attempts = {}  # 予約ID -> 送信試行回数（メモリ上のみ）
        self.post_due.start()

    def cog_unload(self):
        self.post_due.cancel()

    @property
    def _utc_offset(self) -> float:
        return float(self.bot.config.get("utc_offset_hours", 9))

    def _fmt_local(self, ts: float) -> str:
        tz = datetime.timezone(datetime.timedelta(hours=self._utc_offset))
        return datetime.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")

    # ------------------------------ 投稿処理 ------------------------------ #
    @tasks.loop(seconds=30)
    async def post_due(self):
        await self.process_due(time.time())

    @post_due.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def process_due(self, now: float):
        """実行時刻を過ぎた予約を処理する（テストから直接呼べるようループと分離）。"""
        for item in self.bot.store.due_scheduled(now, ACTION):
            try:
                await self._post_one(item, now)
            except Exception as exc:  # 1件の失敗で他の予約やループが止まらないようにする
                # 一時的なエラーで投稿が消えないよう、数回までは残して再試行し、それでも失敗したら通知して破棄する
                tries = self._attempts.get(item["id"], 0) + 1
                print(f"予約投稿の処理に失敗({tries}/{MAX_SEND_ATTEMPTS}): id={item['id']}: {exc!r}")
                if tries < MAX_SEND_ATTEMPTS:
                    self._attempts[item["id"]] = tries
                    continue
                self.bot.store.remove_scheduled(item["id"])
                self._attempts.pop(item["id"], None)
                guild = self.bot.get_guild(item["guild_id"])
                if guild is not None:
                    await h.send_log(self.bot, guild, "mod_log_channel", h.error_embed(
                        "予約投稿の失敗", f"予約 #{item['id']} を投稿できませんでした（予期しないエラー）。内容を確認して、もう一度予約してください。"))

    async def _post_one(self, item: dict, now: float):
        store = self.bot.store
        guild = self.bot.get_guild(item["guild_id"])
        data = item["payload"]
        late = now - item["run_at"]
        if guild is None:
            # 一時的に見えないだけの可能性があるので猶予内は残すが、遅れすぎた予約は退出済みとみなして破棄する
            if late > LATE_LIMIT:
                store.remove_scheduled(item["id"])
                self._attempts.pop(item["id"], None)
            return

        async def fail(reason: str):
            store.remove_scheduled(item["id"])
            self._attempts.pop(item["id"], None)
            msg = f"予約 #{item['id']}（{self._fmt_local(item['run_at'])}）を投稿できませんでした: {reason}"
            await h.send_log(self.bot, guild, "mod_log_channel", h.error_embed("予約投稿の失敗", msg))
            # モデログが未設定でも気づけるよう、予約した本人にも伝える
            member = guild.get_member(item["user_id"])
            if member is not None:
                try:
                    await member.send(embed=h.error_embed("予約投稿を実行できませんでした", f"{guild.name}: {msg}"))
                except discord.HTTPException:
                    pass

        if late > LATE_LIMIT:
            return await fail("Bot の停止などで予定時刻から1時間以上遅れたため、投稿しませんでした。")
        channel = guild.get_channel(data["channel_id"])
        if channel is None:
            return await fail("投稿先チャンネルが見つかりません（削除された可能性があります）。")

        mentions = (discord.AllowedMentions.all() if data.get("mention_ok")
                    else discord.AllowedMentions(everyone=False, roles=False, users=True))
        kwargs = {"allowed_mentions": mentions}
        if data.get("as_embed"):
            kwargs["embed"] = h.embed(f"📢 {data['title']}" if data.get("title") else "📢 お知らせ", data["content"], h.INFO)
        else:
            kwargs["content"] = data["content"]
        try:
            await channel.send(**kwargs)
        except discord.Forbidden:
            return await fail("Bot にそのチャンネルへの送信権限がありません。")
        except discord.HTTPException as exc:
            tries = self._attempts.get(item["id"], 0) + 1
            if exc.status >= 500 and tries < MAX_SEND_ATTEMPTS:
                self._attempts[item["id"]] = tries  # 次のループで再試行
                return
            return await fail(f"Discord エラー {exc.status}: {exc.text}")
        store.remove_scheduled(item["id"])
        self._attempts.pop(item["id"], None)

    # ------------------------------ コマンド ------------------------------ #
    group = h.PermGroup(name="予約投稿", description="🕒 日時を指定してメッセージを自動投稿（権限: メッセージの管理）", required={"manage_messages": True})

    @group.command(name="追加", description="メッセージの予約投稿を登録")
    @app_commands.rename(when="日時", channel="チャンネル", message="本文", as_embed="埋め込み", title="タイトル")
    @app_commands.describe(
        when="投稿日時 例: 30m / 2h / 18:30 / 10-25 18:30（時刻は設定した時差。初期値は日本時間）",
        channel="投稿先のチャンネル",
        message="本文（\\n で改行）",
        as_embed="ON でお知らせ風の埋め込みにする（省略時: OFF）",
        title="埋め込みのタイトル（埋め込みが ON のときだけ有効）",
    )
    async def add(self, interaction: discord.Interaction, when: str, channel: discord.TextChannel,
                  message: app_commands.Range[str, 1, 1900], as_embed: bool = False,
                  title: app_commands.Range[str, 1, 200] = None):
        guild = interaction.guild
        now = datetime.datetime.now(datetime.timezone.utc)
        run_at = h.parse_when(when, now, self._utc_offset)
        if run_at is None:
            return await interaction.response.send_message(embed=h.error_embed(
                "日時エラー", "例: `30m` `2h` `18:30` `10-25 18:30` `2026-10-25 18:30`"), ephemeral=True)
        if run_at < now + MIN_LEAD or run_at > now + MAX_LEAD:
            return await interaction.response.send_message(embed=h.error_embed(
                "日時エラー", "予約できるのは30秒後〜1年後までです。"), ephemeral=True)
        perms = channel.permissions_for(interaction.user)
        if not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(embed=h.error_embed(
                "権限不足", "あなたはそのチャンネルに投稿できません。"), ephemeral=True)
        bot_perms = channel.permissions_for(guild.me)
        if not (bot_perms.view_channel and bot_perms.send_messages):
            return await interaction.response.send_message(embed=h.error_embed(
                "権限不足", "Bot がそのチャンネルに投稿できません。"), ephemeral=True)
        if len(self.bot.store.list_scheduled(guild.id, ACTION)) >= MAX_PENDING_PER_GUILD:
            return await interaction.response.send_message(embed=h.error_embed(
                "上限", f"未投稿の予約は {MAX_PENDING_PER_GUILD} 件までです。"), ephemeral=True)

        content = message.replace("\\n", "\n")
        sched_id = self.bot.store.add_scheduled(guild.id, interaction.user.id, ACTION, run_at.timestamp(), {
            "channel_id": channel.id, "content": content, "as_embed": as_embed, "title": title,
            # @everyone / ロールのメンション権限は、予約した本人が持っている場合のみ有効にする
            "mention_ok": perms.mention_everyone,
        })
        ts = int(run_at.timestamp())
        await interaction.response.send_message(embed=h.ok_embed(
            "予約しました", f"予約ID **#{sched_id}**\n投稿先: {channel.mention}\n日時: <t:{ts}:F>（<t:{ts}:R>）"), ephemeral=True)

    @group.command(name="一覧", description="未投稿の予約を一覧表示")
    async def list_(self, interaction: discord.Interaction):
        items = self.bot.store.list_scheduled(interaction.guild.id, ACTION)
        if not items:
            return await interaction.response.send_message(embed=h.embed("予約投稿", "未投稿の予約はありません。", h.INFO), ephemeral=True)
        e = h.embed(f"🕒 予約投稿（{len(items)}件）", color=h.INFO)
        for it in items[:25]:
            d = it["payload"]
            preview = d["content"].replace("\n", " ")[:80]
            e.add_field(name=f"#{it['id']} — <t:{int(it['run_at'])}:f>",
                        value=f"<#{d['channel_id']}> / 予約者 <@{it['user_id']}>\n{preview}", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def _pending_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.permissions.manage_messages:
            return []  # コマンドの表示設定が上書きされていても、予約の内容を見せない
        items = self.bot.store.list_scheduled(interaction.guild.id, ACTION)
        out = []
        for it in items:
            label = f"#{it['id']} {self._fmt_local(it['run_at'])} {it['payload'].get('content', '')[:40]}"
            if current in str(it["id"]) or current in label:
                out.append(app_commands.Choice(name=label[:100], value=it["id"]))
        return out[:25]

    @group.command(name="取消", description="予約を取り消し")
    @app_commands.rename(schedule_id="予約id")
    @app_commands.describe(schedule_id="取り消す予約のID（候補から選べます）")
    @app_commands.autocomplete(schedule_id=_pending_autocomplete)
    async def cancel(self, interaction: discord.Interaction, schedule_id: int):
        # 他サーバーの予約を消せないよう、このサーバーの予約の中から探す
        mine = {it["id"] for it in self.bot.store.list_scheduled(interaction.guild.id, ACTION)}
        if schedule_id not in mine:
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", f"予約 #{schedule_id} は存在しません。"), ephemeral=True)
        self.bot.store.remove_scheduled(schedule_id)
        await interaction.response.send_message(embed=h.ok_embed("取消", f"予約 #{schedule_id} を取り消しました。"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Schedule(bot))
