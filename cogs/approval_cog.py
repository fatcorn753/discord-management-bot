"""サーバーへの追加を、Bot の持ち主が承認する仕組み（DM のボタン）と、許可の管理コマンド。

Bot が新しいサーバーに追加されると、持ち主（owner_ids）に DM が届き、【許可する】【断って退出させる】を選べる。
許可すると、再起動なしですぐ使えるようになる。ボタンは Bot を再起動しても動く（DynamicItem）。
"""

import time

import discord
from discord import app_commands
from discord.ext import commands

from core import approval
from core import helpers as h

RENOTIFY_AFTER = 3600       # 承認待ちのサーバーで誰かがコマンドを使ったとき、前の通知からこれ以上たっていれば催促する（秒）


def valid_guild_id(text: str) -> bool:
    """サーバーIDとして正しい文字列（半角数字で、SQLite の整数に収まる正の値）。"""
    t = text.strip()
    return t.isascii() and t.isdigit() and 1 <= int(t) < 2 ** 63


class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"guild-approve:(?P<action>allow|deny):(?P<gid>[0-9]+)"):
    def __init__(self, action: str, guild_id: int):
        allow = action == "allow"
        super().__init__(discord.ui.Button(
            label="許可する" if allow else "断って退出させる",
            style=discord.ButtonStyle.success if allow else discord.ButtonStyle.danger,
            custom_id=f"guild-approve:{action}:{guild_id}"))
        self.action, self.guild_id = action, guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], int(match["gid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in approval.owner_ids(interaction.client.config):
            await interaction.response.send_message("これは Bot の持ち主だけが操作できます。", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        bot, store, gid = interaction.client, interaction.client.store, self.guild_id
        guild = bot.get_guild(gid)
        row = store.get_guild_status(gid) or {}
        name = guild.name if guild else row.get("name") or str(gid)
        if self.action == "allow":
            store.set_guild_status(gid, approval.APPROVED, name, interaction.user.id)
            embed = h.ok_embed("許可しました", f"**{name}**（`{gid}`）で使えるようになりました。")
            if guild is not None:
                cog = bot.get_cog("ApprovalCog")
                if cog is not None:
                    await cog.announce_approved(guild)
        else:
            store.set_guild_status(gid, approval.DENIED, name, interaction.user.id)
            left = ""
            if guild is not None:
                try:
                    await guild.leave()
                    left = "Bot はそのサーバーから退出しました。"
                except discord.HTTPException:
                    left = "退出できませんでした（コマンドは使えないままです）。"
            embed = h.warn_embed("断りました", f"**{name}**（`{gid}`）の利用を断りました。{left}")
        await interaction.response.edit_message(embed=embed, view=None)


class ApprovalCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(ApprovalButton)

    @property
    def store(self):
        return self.bot.store

    # --------------------------- 依頼の送信 --------------------------- #
    async def request_approval(self, guild: discord.Guild) -> int:
        """持ち主に承認を依頼する DM を送る。届いた人数を返す（DM を受け取れない人には届かない）。"""
        self.store.mark_guild_notified(guild.id)        # 送信（await）の前に記録する。同時に来た別のコマンドが、重ねて依頼しないように
        embed = h.embed("🆕 新しいサーバーに追加されました", color=h.WARN)
        embed.add_field(name="サーバー", value=f"{guild.name}\n`{guild.id}`", inline=True)
        embed.add_field(name="持ち主", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="メンバー数", value=f"{guild.member_count or '?'}人", inline=True)
        embed.set_footer(text="許可するまで、このサーバーでは Bot の機能が動きません")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        view = discord.ui.View(timeout=None)
        view.add_item(ApprovalButton("allow", guild.id))
        view.add_item(ApprovalButton("deny", guild.id))
        delivered = 0
        for uid in approval.owner_ids(self.bot.config):
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                await user.send(embed=embed, view=view)
                delivered += 1
            except discord.HTTPException as exc:
                print(f"承認の依頼を DM で送れません: owner={uid}: {exc!r}")
        return delivered

    async def ensure_requested(self, guild: discord.Guild, reset_denied: bool = False) -> None:
        """承認待ちとして記録し、まだ依頼していなければ依頼する。しばらく前の依頼なら催促する。"""
        if guild is None or guild.id in approval.preapproved(self.bot.config):
            return
        row = self.store.get_guild_status(guild.id)
        if row is None or (row["status"] == approval.DENIED and reset_denied):
            self.store.set_guild_status(guild.id, approval.PENDING, guild.name)
            await self.request_approval(guild)
        elif row["status"] == approval.PENDING:
            if row["notified_at"] is None or time.time() - row["notified_at"] > RENOTIFY_AFTER:
                await self.request_approval(guild)

    async def announce_approved(self, guild: discord.Guild) -> None:
        """承認されたことを、サーバー側にも知らせる（できなければ何もしない）。"""
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        if channel is None:
            return
        try:
            await channel.send(embed=h.ok_embed("使えるようになりました", "Bot の持ち主に承認されました。`/ヘルプ` で使えるコマンドを確認できます。"))
        except discord.HTTPException:
            pass

    # ----------------------------- イベント ----------------------------- #
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if not approval.requires_approval(self.bot.config):
            return
        row = self.store.get_guild_status(guild.id)
        if row and row["status"] == approval.APPROVED:
            return                          # 承認済みのサーバーに追加し直された
        await self.ensure_requested(guild, reset_denied=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if not approval.requires_approval(self.bot.config):
            return
        for guild in self.bot.guilds:        # Bot が止まっている間に追加されたサーバーも拾う
            await self.ensure_requested(guild)

    # ------------------- 許可の管理（Bot の持ち主だけ） ------------------- #
    group = app_commands.Group(
        name="運営", description="（持ち主のみ）Bot を使えるサーバーの管理。Bot とのDMで使えます",
        allowed_contexts=app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=True))

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in approval.owner_ids(self.bot.config):
            await interaction.response.send_message(embed=h.error_embed("権限がありません", "このコマンドは Bot の持ち主だけが使えます。"), ephemeral=True)
            return False
        return True

    @group.command(name="サーバー一覧", description="（持ち主のみ）許可・承認待ち・断ったサーバーを表示する")
    async def server_list(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        joined = {g.id: g for g in self.bot.guilds}
        rows = {r["guild_id"]: r for r in self.store.list_guild_status()}
        pre = approval.preapproved(self.bot.config)
        groups = {"✅ 許可": [], "⏳ 承認待ち": [], "🚫 断った": []}
        for gid in pre:
            groups["✅ 許可"].append((gid, joined[gid].name if gid in joined else "（設定ファイルで許可）", gid in joined))
        for gid, r in rows.items():
            if gid in pre:
                continue
            key = {approval.APPROVED: "✅ 許可", approval.PENDING: "⏳ 承認待ち", approval.DENIED: "🚫 断った"}.get(r["status"])
            if key:
                groups[key].append((gid, joined[gid].name if gid in joined else (r["name"] or "?"), gid in joined))
        e = h.embed("🌐 サーバーの許可状況", f"承認制: **{'ON' if approval.requires_approval(self.bot.config) else 'OFF'}**　参加中: {len(joined)}サーバー", h.INFO)
        for title, items in groups.items():
            e.add_field(name=f"{title}（{len(items)}）", value="\n".join(f"{'🟢' if on else '⚪'} {name}　`{gid}`" for gid, name, on in items)[:1024] or "なし", inline=False)
        e.set_footer(text="🟢 = いま参加中 / ⚪ = 参加していない")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="サーバー許可", description="（持ち主のみ）サーバーIDを指定して、利用を許可する")
    @app_commands.rename(guild_id="サーバーid")
    @app_commands.describe(guild_id="許可するサーバーのID（承認待ちの一覧にあるもの）")
    async def server_allow(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_only(interaction):
            return
        if not valid_guild_id(guild_id):
            return await interaction.response.send_message(embed=h.error_embed("入力エラー", "サーバーidは数字だけで入力してください。"), ephemeral=True)
        gid = int(guild_id)
        guild = self.bot.get_guild(gid)
        self.store.set_guild_status(gid, approval.APPROVED, guild.name if guild else None, interaction.user.id)
        if guild is not None:
            await self.announce_approved(guild)
        await interaction.response.send_message(embed=h.ok_embed("許可しました", f"`{gid}`" + (f"（{guild.name}）" if guild else "（まだ参加していません。追加されたらすぐ使えます）") + " で使えます。"), ephemeral=True)

    @group.command(name="サーバー取消", description="（持ち主のみ）許可を取り消し、Bot をそのサーバーから退出させる")
    @app_commands.rename(guild_id="サーバーid")
    @app_commands.describe(guild_id="取り消すサーバーのID")
    async def server_revoke(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_only(interaction):
            return
        if not valid_guild_id(guild_id):
            return await interaction.response.send_message(embed=h.error_embed("入力エラー", "サーバーidは数字だけで入力してください。"), ephemeral=True)
        gid = int(guild_id)
        if gid in approval.preapproved(self.bot.config):
            return await interaction.response.send_message(embed=h.warn_embed("取り消せません", "設定ファイル（allowed_guild_ids）で許可されているサーバーです。設定ファイルから外してください。"), ephemeral=True)
        guild = self.bot.get_guild(gid)
        self.store.set_guild_status(gid, approval.DENIED, guild.name if guild else None, interaction.user.id)
        left = ""
        if guild is not None:
            try:
                await guild.leave()
                left = "Bot はそのサーバーから退出しました。"
            except discord.HTTPException:
                left = "退出はできませんでしたが、機能は使えなくなりました。"
        await interaction.response.send_message(embed=h.ok_embed("取り消しました", f"`{gid}` の許可を取り消しました。{left}"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(ApprovalCog(bot))
