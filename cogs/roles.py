"""リアクションロール: メッセージへの絵文字リアクションでロールを付与/剥奪。"""

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h


def emoji_key(payload_emoji: discord.PartialEmoji) -> str:
    """カスタム絵文字は <:name:id> 形式、標準絵文字は文字そのものをキーにする。"""
    if payload_emoji.id:
        return f"{payload_emoji.name}:{payload_emoji.id}"
    return str(payload_emoji.name)


class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    role_group = h.PermGroup(name="ロール", description="🎭 ロールの付与・剥奪とリアクションロール（権限: ロールの管理）", required={"manage_roles": True})

    @staticmethod
    def _check_role(interaction: discord.Interaction, role: discord.Role, verb: str):
        """付与/剥奪できないロールなら理由を返す。"""
        if role.is_default() or role.managed:
            return "@everyone や連携用のロールは操作できません。"
        if role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
            return f"自分と同じか上位のロールは{verb}できません。"
        if role >= interaction.guild.me.top_role:
            return f"Bot より上位のロールは{verb}できません。Bot のロールを上へ動かしてください。"
        return None

    @role_group.command(name="付与", description="メンバーにロールを付与")
    @app_commands.rename(member="対象", role="ロール")
    @app_commands.describe(member="付与するメンバー", role="付与するロール")
    async def role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        err = self._check_role(interaction, role, "付与")
        if err:
            return await interaction.response.send_message(embed=h.error_embed("実行できません", err), ephemeral=True)
        if role in member.roles:
            return await interaction.response.send_message(embed=h.warn_embed("付与済みです", f"{member.mention} はすでに {role.mention} を持っています。"), ephemeral=True)
        await member.add_roles(role, reason=f"{interaction.user}")
        await interaction.response.send_message(embed=h.ok_embed("付与しました", f"{member.mention} に {role.mention} を付与しました。"))

    @role_group.command(name="剥奪", description="メンバーからロールを外す")
    @app_commands.rename(member="対象", role="ロール")
    @app_commands.describe(member="外すメンバー", role="外すロール")
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        err = self._check_role(interaction, role, "操作")
        if err:
            return await interaction.response.send_message(embed=h.error_embed("実行できません", err), ephemeral=True)
        if role not in member.roles:
            return await interaction.response.send_message(embed=h.warn_embed("持っていません", f"{member.mention} は {role.mention} を持っていません。"), ephemeral=True)
        await member.remove_roles(role, reason=f"{interaction.user}")
        await interaction.response.send_message(embed=h.ok_embed("外しました", f"{member.mention} から {role.mention} を外しました。"))

    @role_group.command(name="リアクション登録", description="メッセージへのリアクションでロールを付与する設定を追加")
    @app_commands.rename(message_id="メッセージid", emoji="絵文字", role="ロール", channel="チャンネル")
    @app_commands.describe(message_id="対象メッセージのID（メッセージを右クリック→IDをコピー。開発者モードが必要）",
                           emoji="リアクションに使う絵文字", role="付与するロール",
                           channel="メッセージがあるチャンネル（省略時: このチャンネル）")
    async def add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role,
                  channel: discord.TextChannel = None):
        channel = channel or interaction.channel
        try:
            mid = int(message_id)
            message = await channel.fetch_message(mid)
        except (ValueError, discord.NotFound):
            return await interaction.response.send_message(embed=h.error_embed("メッセージが見つかりません", f"{channel.mention} にそのIDのメッセージがありません。別のチャンネルなら「チャンネル」も指定してください。"), ephemeral=True)
        except discord.Forbidden:
            return await interaction.response.send_message(embed=h.error_embed("読み取れません", "Bot がそのチャンネルのメッセージを読めません。権限を確認してください。"), ephemeral=True)
        err = self._check_role(interaction, role, "付与")
        if err:
            return await interaction.response.send_message(embed=h.error_embed("設定できません", err), ephemeral=True)
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            return await interaction.response.send_message(embed=h.error_embed("絵文字を付けられません", "Bot が使える絵文字か確認してください（他サーバーの絵文字は使えないことがあります）。"), ephemeral=True)

        # 保存キーを PartialEmoji ベースに正規化
        pe = discord.PartialEmoji.from_str(emoji)
        key = emoji_key(pe)
        self.bot.store.add_reaction_role(interaction.guild.id, mid, key, role.id)
        await interaction.response.send_message(embed=h.ok_embed("登録しました", f"{emoji} → {role.mention}\n{message.jump_url}"), ephemeral=True)

    async def _reaction_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.permissions.manage_roles:
            return []
        rows = self.bot.store.list_reaction_roles(interaction.guild.id)
        seen, choices = set(), []
        for r in rows:
            mid = str(r["message_id"])
            if mid in seen or current not in mid:
                continue
            seen.add(mid)
            choices.append(app_commands.Choice(name=f"{mid}（{r['emoji']}ほか）"[:100], value=mid))
        return choices[:25]

    @role_group.command(name="リアクション削除", description="リアクションロールの設定を削除")
    @app_commands.rename(message_id="メッセージid", emoji="絵文字")
    @app_commands.describe(message_id="対象メッセージのID（候補から選べます）", emoji="削除する設定の絵文字")
    @app_commands.autocomplete(message_id=_reaction_autocomplete)
    async def remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=h.error_embed("入力エラー", "メッセージidは数字だけで入力してください。"), ephemeral=True)
        pe = discord.PartialEmoji.from_str(emoji)
        ok = self.bot.store.remove_reaction_role(interaction.guild.id, mid, emoji_key(pe))
        if not ok:
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", "その設定は登録されていません。`/ロール リアクション一覧` で確認できます。"), ephemeral=True)
        await interaction.response.send_message(embed=h.ok_embed("削除しました", "リアクションロールの設定を削除しました。"), ephemeral=True)

    @role_group.command(name="リアクション一覧", description="登録済みのリアクションロールを一覧表示")
    async def list_reactions(self, interaction: discord.Interaction):
        rows = self.bot.store.list_reaction_roles(interaction.guild.id)
        if not rows:
            return await interaction.response.send_message(embed=h.embed("リアクションロール", "登録されている設定はありません。", h.INFO), ephemeral=True)
        lines = []
        for r in rows[:40]:
            emoji = r["emoji"].split(":")[0] if ":" in r["emoji"] else r["emoji"]
            lines.append(f"・{emoji} → <@&{r['role_id']}>　メッセージid `{r['message_id']}`")
        more = f"\n…ほか {len(rows) - 40} 件" if len(rows) > 40 else ""
        await interaction.response.send_message(embed=h.embed(f"🎭 リアクションロール（{len(rows)}件）", "\n".join(lines) + more, h.INFO), ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return
        role_id = self.bot.store.get_reaction_role(payload.guild_id, payload.message_id, emoji_key(payload.emoji))
        if not role_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(int(role_id)) if guild else None
        if role:
            try:
                await payload.member.add_roles(role, reason="リアクションロール")
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        role_id = self.bot.store.get_reaction_role(payload.guild_id, payload.message_id, emoji_key(payload.emoji))
        if not role_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        role = guild.get_role(int(role_id))
        if member and role and not member.bot:
            try:
                await member.remove_roles(role, reason="リアクションロール解除")
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
