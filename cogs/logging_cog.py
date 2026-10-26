"""イベントログ: メッセージ編集/削除・入退室・ニックネーム/ロール変更・ボイス。

送信先はギルド設定 log_channel（/設定 ログ で指定）。
"""

import discord
from discord.ext import commands

from core import helpers as h


class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send(self, guild, e):
        await h.send_log(self.bot, guild, "log_channel", e)

    # --------------------------- メッセージ --------------------------- #
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        e = h.embed("🗑️ メッセージ削除", color=h.ERROR)
        e.add_field(name="投稿者", value=f"{message.author.mention}", inline=True)
        e.add_field(name="チャンネル", value=message.channel.mention, inline=True)
        if message.content:
            e.add_field(name="内容", value=message.content[:1024], inline=False)
        if message.attachments:
            e.add_field(name="添付", value="\n".join(a.filename for a in message.attachments), inline=False)
        e.set_footer(text=f"ユーザーID: {message.author.id}")
        await self._send(message.guild, e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot or before.content == after.content:
            return
        e = h.embed("✏️ メッセージ編集", color=h.WARN)
        e.add_field(name="投稿者", value=before.author.mention, inline=True)
        e.add_field(name="チャンネル", value=f"{before.channel.mention} [ジャンプ]({after.jump_url})", inline=True)
        e.add_field(name="変更前", value=(before.content or "（なし）")[:1024], inline=False)
        e.add_field(name="変更後", value=(after.content or "（なし）")[:1024], inline=False)
        await self._send(before.guild, e)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages):
        if not messages or messages[0].guild is None:
            return
        e = h.embed("🗑️ メッセージ一括削除", f"{len(messages)} 件が削除されました", h.ERROR)
        e.add_field(name="チャンネル", value=messages[0].channel.mention, inline=True)
        await self._send(messages[0].guild, e)

    # ----------------------------- 入退室 ----------------------------- #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        e = h.embed("📥 メンバー参加", color=h.OK)
        e.add_field(name="ユーザー", value=f"{member.mention} ({member})", inline=False)
        e.add_field(name="アカウント作成", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        e.add_field(name="現在の人数", value=str(member.guild.member_count), inline=True)
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"ユーザーID: {member.id}")
        await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        e = h.embed("📤 メンバー退出", color=h.ERROR)
        e.add_field(name="ユーザー", value=f"{member} (`{member.id}`)", inline=False)
        e.add_field(name="参加日時", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "不明", inline=True)
        if roles:
            e.add_field(name="所持ロール", value=" ".join(roles)[:1024], inline=False)
        e.set_thumbnail(url=member.display_avatar.url)
        await self._send(member.guild, e)

    # ------------------------- メンバー更新 --------------------------- #
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            e = h.embed("📝 ニックネーム変更", color=h.LOG)
            e.add_field(name="ユーザー", value=after.mention, inline=False)
            e.add_field(name="変更前", value=before.nick or "（なし）", inline=True)
            e.add_field(name="変更後", value=after.nick or "（なし）", inline=True)
            await self._send(after.guild, e)
        if before.roles != after.roles:
            added = set(after.roles) - set(before.roles)
            removed = set(before.roles) - set(after.roles)
            if added or removed:
                e = h.embed("🎭 ロール変更", color=h.LOG)
                e.add_field(name="ユーザー", value=after.mention, inline=False)
                if added:
                    e.add_field(name="追加", value=" ".join(r.mention for r in added), inline=False)
                if removed:
                    e.add_field(name="削除", value=" ".join(r.mention for r in removed), inline=False)
                await self._send(after.guild, e)

    # ----------------------------- ボイス ----------------------------- #
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        e = h.embed("🔊 ボイス状態", color=h.LOG)
        e.add_field(name="ユーザー", value=member.mention, inline=False)
        if before.channel is None:
            e.description = f"{after.channel.name} に参加"
        elif after.channel is None:
            e.description = f"{before.channel.name} から退出"
        else:
            e.description = f"{before.channel.name} → {after.channel.name} へ移動"
        await self._send(member.guild, e)


async def setup(bot):
    await bot.add_cog(Logging(bot))
