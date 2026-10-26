"""情報表示: /情報 ユーザー・サーバー・ロール・アバター・応答速度。"""

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    info = app_commands.Group(name="情報", description="🔍 ユーザー・サーバー・ロールなどの情報表示")

    @info.command(name="応答速度", description="Bot の応答速度を表示")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(embed=h.embed("🏓 Pong!", f"レイテンシ: **{latency}ms**", h.INFO))

    @info.command(name="ユーザー", description="メンバーの情報を表示")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="見たいメンバー（省略時: 自分）")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
        e = h.embed(f"👤 {member.display_name} の情報", color=member.color if member.color.value else h.INFO)
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="ユーザー名", value=str(member), inline=True)
        e.add_field(name="ID", value=str(member.id), inline=True)
        e.add_field(name="Bot", value="はい" if member.bot else "いいえ", inline=True)
        e.add_field(name="アカウント作成", value=discord.utils.format_dt(member.created_at, "F"), inline=False)
        if member.joined_at:
            e.add_field(name="サーバー参加", value=discord.utils.format_dt(member.joined_at, "F"), inline=False)
        e.add_field(name="最上位ロール", value=member.top_role.mention, inline=True)
        if member.premium_since:
            e.add_field(name="ブースト開始", value=discord.utils.format_dt(member.premium_since, "R"), inline=True)
        e.add_field(name=f"ロール ({len(roles)})", value=" ".join(roles)[:1024] if roles else "なし", inline=False)
        # 他人の情報は本人に見せたくない場合があるため、自分以外を調べたときは自分にだけ表示する
        await interaction.response.send_message(embed=e, ephemeral=member.id != interaction.user.id)

    @info.command(name="サーバー", description="サーバー情報を表示")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        text_ch = len(g.text_channels)
        voice_ch = len(g.voice_channels)
        humans = sum(1 for m in g.members if not m.bot)
        bots = g.member_count - humans
        e = h.embed(f"🏠 {g.name}", color=h.INFO)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name="オーナー", value=g.owner.mention if g.owner else "不明", inline=True)
        e.add_field(name="サーバーID", value=str(g.id), inline=True)
        e.add_field(name="作成日", value=discord.utils.format_dt(g.created_at, "D"), inline=True)
        e.add_field(name="メンバー", value=f"合計 {g.member_count}\n人間 {humans} / Bot {bots}", inline=True)
        e.add_field(name="チャンネル", value=f"💬 {text_ch} / 🔊 {voice_ch}", inline=True)
        e.add_field(name="ロール数", value=str(len(g.roles)), inline=True)
        e.add_field(name="ブースト", value=f"Lv{g.premium_tier} ({g.premium_subscription_count}件)", inline=True)
        e.add_field(name="絵文字", value=str(len(g.emojis)), inline=True)
        await interaction.response.send_message(embed=e)

    @info.command(name="アバター", description="アバター画像を表示")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="見たいメンバー（省略時: 自分）")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        e = h.embed(f"🖼️ {member.display_name} のアバター", color=h.INFO)
        e.set_image(url=member.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @info.command(name="ロール", description="ロール情報を表示")
    @app_commands.rename(role="ロール")
    @app_commands.describe(role="見たいロール")
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        e = h.embed(f"🎭 {role.name}", color=role.color if role.color.value else h.INFO)
        e.add_field(name="ID", value=str(role.id), inline=True)
        e.add_field(name="メンバー数", value=str(len(role.members)), inline=True)
        e.add_field(name="色", value=str(role.color), inline=True)
        e.add_field(name="別表示", value="はい" if role.hoist else "いいえ", inline=True)
        e.add_field(name="メンション可", value="はい" if role.mentionable else "いいえ", inline=True)
        e.add_field(name="作成日", value=discord.utils.format_dt(role.created_at, "D"), inline=True)
        await interaction.response.send_message(embed=e)


async def setup(bot):
    await bot.add_cog(Utility(bot))
