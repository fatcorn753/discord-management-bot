"""入退室メッセージ + 参加時の自動ロール付与。

ギルド設定:
  welcome_channel : int   参加メッセージの送信先
  welcome_message : str   {user}{mention}{server}{count} を置換
  goodbye_channel : int   退出メッセージの送信先
  goodbye_message : str
  autorole        : int   参加時に自動付与するロールID
"""

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h

DEFAULT_WELCOME = "ようこそ {mention} さん！ **{server}** へ！ 🎉\nあなたは **{count}** 人目のメンバーです。"
DEFAULT_GOODBYE = "{user} さんが退出しました。またね 👋"


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _fmt(self, template, member):
        return (template
                .replace("\\n", "\n")  # スラッシュコマンドは改行を入力できないため、\n を改行として扱う
                .replace("{mention}", member.mention)
                .replace("{user}", str(member))
                .replace("{server}", member.guild.name)
                .replace("{count}", str(member.guild.member_count)))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        gid = member.guild.id
        # 自動ロール
        role_id = self.bot.store.get_setting(gid, "autorole")
        if role_id:
            role = member.guild.get_role(int(role_id))
            if role:
                try:
                    await member.add_roles(role, reason="自動ロール付与")
                except discord.HTTPException:
                    pass
        # ようこそメッセージ
        ch_id = self.bot.store.get_setting(gid, "welcome_channel")
        if ch_id:
            ch = member.guild.get_channel(int(ch_id))
            if ch:
                template = self.bot.store.get_setting(gid, "welcome_message", DEFAULT_WELCOME)
                e = h.embed("👋 ようこそ！", self._fmt(template, member), h.OK)
                e.set_thumbnail(url=member.display_avatar.url)
                try:
                    await ch.send(embed=e)
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        ch_id = self.bot.store.get_setting(member.guild.id, "goodbye_channel")
        if ch_id:
            ch = member.guild.get_channel(int(ch_id))
            if ch:
                template = self.bot.store.get_setting(member.guild.id, "goodbye_message", DEFAULT_GOODBYE)
                e = h.embed("さようなら", self._fmt(template, member), h.LOG)
                try:
                    await ch.send(embed=e)
                except discord.HTTPException:
                    pass

    # --------------------------- 設定コマンド -------------------------- #
    welcome = h.PermGroup(name="入退室", description="👋 入退室メッセージと参加時の自動ロール（権限: サーバー管理）", required={"manage_guild": True})

    @welcome.command(name="参加設定", description="参加メッセージの送信先と文面を設定")
    @app_commands.rename(channel="チャンネル", message="メッセージ")
    @app_commands.describe(
        channel="送信先のチャンネル（省略時: 変更なし）",
        message="文面。{mention} {user} {server} {count} が使えます。\\n で改行（省略時: 変更なし）")
    async def set_welcome(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                          message: app_commands.Range[str, 1, 1500] = None):
        if channel is None and message is None:
            return await interaction.response.send_message(embed=h.error_embed("指定がありません", "「チャンネル」か「メッセージ」のどちらかを指定してください。"), ephemeral=True)
        gid = interaction.guild.id
        if channel:
            self.bot.store.set_setting(gid, "welcome_channel", channel.id)
        if message:
            self.bot.store.set_setting(gid, "welcome_message", message)
        ch_id = self.bot.store.get_setting(gid, "welcome_channel")
        text = f"送信先: {'<#%s>' % ch_id if ch_id else '未設定（チャンネルも指定してください）'}\n`/入退室 プレビュー` で見え方を確認できます。"
        await interaction.response.send_message(embed=h.ok_embed("参加メッセージを設定しました", text), ephemeral=True)

    @welcome.command(name="退出設定", description="退出メッセージの送信先と文面を設定")
    @app_commands.rename(channel="チャンネル", message="メッセージ")
    @app_commands.describe(
        channel="送信先のチャンネル（省略時: 変更なし）",
        message="文面。使える置換語は参加設定と同じ（省略時: 変更なし）")
    async def set_goodbye(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                          message: app_commands.Range[str, 1, 1500] = None):
        if channel is None and message is None:
            return await interaction.response.send_message(embed=h.error_embed("指定がありません", "「チャンネル」か「メッセージ」のどちらかを指定してください。"), ephemeral=True)
        gid = interaction.guild.id
        if channel:
            self.bot.store.set_setting(gid, "goodbye_channel", channel.id)
        if message:
            self.bot.store.set_setting(gid, "goodbye_message", message)
        ch_id = self.bot.store.get_setting(gid, "goodbye_channel")
        text = f"送信先: {'<#%s>' % ch_id if ch_id else '未設定（チャンネルも指定してください）'}"
        await interaction.response.send_message(embed=h.ok_embed("退出メッセージを設定しました", text), ephemeral=True)

    @welcome.command(name="参加解除", description="参加メッセージの送信を停止")
    async def clear_welcome(self, interaction: discord.Interaction):
        self.bot.store.delete_setting(interaction.guild.id, "welcome_channel")
        await interaction.response.send_message(embed=h.ok_embed("停止しました", "参加メッセージの送信を停止しました（文面は残してあります）。"), ephemeral=True)

    @welcome.command(name="退出解除", description="退出メッセージの送信を停止")
    async def clear_goodbye(self, interaction: discord.Interaction):
        self.bot.store.delete_setting(interaction.guild.id, "goodbye_channel")
        await interaction.response.send_message(embed=h.ok_embed("停止しました", "退出メッセージの送信を停止しました（文面は残してあります）。"), ephemeral=True)

    @welcome.command(name="自動ロール", description="参加時に自動で付与するロールを設定")
    @app_commands.rename(role="ロール")
    @app_commands.describe(role="付与するロール（省略時: 自動ロールを解除）")
    async def set_autorole(self, interaction: discord.Interaction, role: discord.Role = None):
        if role is None:
            self.bot.store.delete_setting(interaction.guild.id, "autorole")
            return await interaction.response.send_message(embed=h.ok_embed("解除しました", "自動ロールを解除しました。"), ephemeral=True)
        problem = h.auto_grant_role_problem(interaction, role)
        if problem:
            return await interaction.response.send_message(embed=h.error_embed("設定できません", problem), ephemeral=True)
        self.bot.store.set_setting(interaction.guild.id, "autorole", role.id)
        text = f"参加時に {role.mention} を付与します。"
        if self.bot.store.get_setting(interaction.guild.id, "verify_role"):
            text += "\n⚠️ 認証ゲートを使っている場合、自動ロールで閲覧権限を与えると認証の意味がなくなります。"
        await interaction.response.send_message(embed=h.ok_embed("設定しました", text), ephemeral=True)

    @welcome.command(name="プレビュー", description="参加メッセージの見え方を自分で確認")
    async def test(self, interaction: discord.Interaction):
        template = self.bot.store.get_setting(interaction.guild.id, "welcome_message", DEFAULT_WELCOME)
        e = h.embed("👋 ようこそ！（プレビュー）", self._fmt(template, interaction.user), h.OK)
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=e, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Welcome(bot))
