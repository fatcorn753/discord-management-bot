"""
認証システム（荒らし対策）。

仕組み:
  新規参加者にはまず何のロールも与えず、@everyone は認証チャンネル以外を
  見られないように設定しておく。参加者が認証パネルの「✅ 認証する」ボタンを
  押すと「認証済みロール」が付与され、初めてサーバーを閲覧できるようになる。
  これにより、参加直後に暴れる荒らし・大量BOTの被害を大幅に抑えられる。

  ボタンは custom_id 固定の永続 View なので Bot 再起動後も機能する。

ギルド設定:
  verify_role        : int   認証で付与するロールID
  verify_min_age_days: int   アカウント作成からの最低日数（0で無効）

（参加時に無条件付与する `/入退室 自動ロール` とは別物。荒らし対策目的なら
  autorole ではなくこちらの認証式を使うことを推奨。）
"""

import datetime

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h


class VerifyView(discord.ui.View):
    """認証パネル（永続）。"""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    def _contact_note(self, guild_id: int) -> str:
        cid = self.bot.store.get_setting(guild_id, "verify_contact")
        if cid:
            return f"\n事情がある場合は <#{cid}> でスタッフにお知らせください。"
        return "\n事情がある場合は、サーバーのスタッフに連絡してください。"

    @discord.ui.button(label="✅ 認証する", style=discord.ButtonStyle.success, custom_id="verify:button")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        role_id = self.bot.store.get_setting(gid, "verify_role")
        if not role_id:
            return await interaction.response.send_message(
                embed=h.error_embed("まだ設定されていません", "認証ロールが設定されていません。サーバーの管理者に連絡してください。"), ephemeral=True)
        role = interaction.guild.get_role(int(role_id))
        if role is None:
            return await interaction.response.send_message(
                embed=h.error_embed("認証できません", "認証ロールが見つかりません。サーバーの管理者に連絡してください。"), ephemeral=True)

        # 既に認証済み
        if role in interaction.user.roles:
            return await interaction.response.send_message(
                embed=h.embed("認証済みです", "あなたは既に認証済みです。", h.OK), ephemeral=True)

        # アカウント作成日数チェック（新規アカウントによる荒らし対策）
        min_age = int(self.bot.store.get_setting(gid, "verify_min_age_days", 0) or 0)
        if min_age > 0:
            created = interaction.user.created_at
            age_days = (discord.utils.utcnow() - created).days
            if age_days < min_age:
                ready = int((created + datetime.timedelta(days=min_age)).timestamp())
                e = h.error_embed(
                    "まだ認証できません",
                    f"このサーバーでは、アカウント作成から {min_age} 日以上たった人が認証できます（現在 {age_days} 日）。\n"
                    f"<t:{ready}:D>（<t:{ready}:R>）以降に、もう一度お試しください。{self._contact_note(gid)}")
                await interaction.response.send_message(embed=e, ephemeral=True)
                # ログに記録
                log = h.embed("🚫 認証の拒否（新しいアカウント）", f"{interaction.user} (`{interaction.user.id}`) 作成 {age_days} 日", h.WARN)
                await h.send_log(self.bot, interaction.guild, "mod_log_channel", log)
                return

        try:
            await interaction.user.add_roles(role, reason="認証システム")
        except discord.Forbidden:
            return await interaction.response.send_message(
                embed=h.error_embed("認証できませんでした", "Bot の権限が足りないため、ロールを付与できません。サーバーの管理者に「Bot のロールを認証ロールより上に置いてください」と伝えてください。"), ephemeral=True)
        except discord.HTTPException:
            return await interaction.response.send_message(
                embed=h.error_embed("認証できませんでした", "Discord 側のエラーで失敗しました。少し待ってから、もう一度押してください。"), ephemeral=True)

        await interaction.response.send_message(
            embed=h.ok_embed("認証しました", f"ようこそ！ {role.mention} を付与しました。サーバーのチャンネルが見られるようになります。まずはルールや案内のチャンネルを確認してみてください。"), ephemeral=True)
        log = h.embed("✅ 認証が完了しました", f"{interaction.user.mention} が認証しました。", h.OK)
        await h.send_log(self.bot, interaction.guild, "log_channel", log)


class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    verify = h.PermGroup(name="認証", description="🔐 荒らし対策の認証ゲート（権限: サーバー管理）", required={"manage_guild": True})

    async def _remove_old_panel(self, guild: discord.Guild):
        """設置し直すたびにパネルが増えないよう、前回のパネルを削除する（失敗は無視）。"""
        old = self.bot.store.get_setting(guild.id, "verify_panel")
        if not old:
            return
        channel = guild.get_channel(int(old["channel_id"]))
        if channel is not None:
            try:
                await (await channel.fetch_message(int(old["message_id"]))).delete()
            except discord.HTTPException:
                pass

    @verify.command(name="設置", description="認証パネルを設置し、認証ロールを設定")
    @app_commands.rename(channel="チャンネル", role="ロール", min_account_age_days="最低日数", contact="連絡先", title="タイトル", description="説明文")
    @app_commands.describe(
        channel="パネルを置くチャンネル",
        role="認証後に付与するロール",
        min_account_age_days="アカウント作成からの最低日数 0〜365（省略時: 制限なし）",
        contact="認証できない人の連絡先チャンネル（省略時: 案内文のみ。@everyone が見える必要あり）",
        title="パネルのタイトル（省略可）",
        description="パネルの説明文（省略可）",
    )
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role,
                        min_account_age_days: app_commands.Range[int, 0, 365] = 0,
                        contact: discord.TextChannel = None,
                        title: app_commands.Range[str, 1, 200] = "サーバー認証",
                        description: app_commands.Range[str, 1, 1000] = "下の「✅ 認証する」ボタンを押すと、サーバーに参加できます。"):
        problem = h.auto_grant_role_problem(interaction, role)
        if problem:
            return await interaction.response.send_message(embed=h.error_embed("設定できません", problem), ephemeral=True)
        bot_perms = channel.permissions_for(interaction.guild.me)
        if not (bot_perms.view_channel and bot_perms.send_messages and bot_perms.embed_links):
            return await interaction.response.send_message(
                embed=h.error_embed("設置できません", f"Bot が {channel.mention} に埋め込みを投稿できません。チャンネルの権限を確認してください。"), ephemeral=True)

        store = self.bot.store
        gid = interaction.guild.id
        store.set_setting(gid, "verify_role", role.id)
        store.set_setting(gid, "verify_min_age_days", min_account_age_days)
        if contact:
            store.set_setting(gid, "verify_contact", contact.id)
        else:
            store.delete_setting(gid, "verify_contact")

        await interaction.response.defer(ephemeral=True)
        e = h.embed(f"🔐 {title}", description, h.INFO)
        if min_account_age_days > 0:
            e.set_footer(text=f"※ アカウント作成から{min_account_age_days}日以上が必要です")
        # 新しいパネルを先に投稿し、成功してから古いものを消す（投稿に失敗して設定だけ残る状態を避ける）
        try:
            msg = await channel.send(embed=e, view=VerifyView(self.bot))
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=h.error_embed("設置できませんでした", f"パネルを投稿できませんでした（コード {exc.status}）。Bot の権限を確認してください。"), ephemeral=True)
        await self._remove_old_panel(interaction.guild)
        store.set_setting(gid, "verify_panel", {"channel_id": channel.id, "message_id": msg.id})

        guide = (
            f"認証パネルを {channel.mention} に設置しました。\n"
            f"認証ロール: {role.mention}\n\n"
            "**仕上げに以下を確認してください（荒らし対策の要）:**\n"
            f"1. `@everyone` の権限で、{channel.mention} 以外のチャンネルを非表示にする\n"
            f"2. {role.mention} に、通常のチャンネルの閲覧権限を与える\n"
            "3. こうすると、認証するまでサーバーが見えなくなります。\n\n"
            "`/認証 状況` で、ロール順位などの設定を確認できます。")
        if contact:
            guide += f"\n連絡先に指定した {contact.mention} は、認証前の人にも見える設定にしてください。"
        await interaction.followup.send(embed=h.ok_embed("設置しました", guide), ephemeral=True)

    @verify.command(name="状況", description="現在の認証設定を確認")
    async def config_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        store = self.bot.store
        role_id = store.get_setting(gid, "verify_role")
        min_age = store.get_setting(gid, "verify_min_age_days", 0)
        contact = store.get_setting(gid, "verify_contact")
        e = h.embed("🔐 認証の設定", color=h.INFO)
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        if not role_id:
            e.add_field(name="認証ロール", value="未設定（`/認証 設置` で設定します）", inline=False)
        elif role is None:
            e.add_field(name="認証ロール", value="⚠️ 設定されたロールが削除されています。`/認証 設置` をやり直してください。", inline=False)
        else:
            e.add_field(name="認証ロール", value=role.mention, inline=True)
            if role >= interaction.guild.me.top_role:
                e.add_field(name="⚠️ ロール順位", value="Bot のロールが認証ロールより下です。付与できないため、Bot のロールを上へ動かしてください。", inline=False)
        e.add_field(name="最低アカウント日数", value=f"{min_age}日" if min_age else "制限なし", inline=True)
        e.add_field(name="連絡先", value=f"<#{contact}>" if contact else "未設定", inline=True)
        panel = store.get_setting(gid, "verify_panel")
        e.add_field(name="パネル", value=f"<#{panel['channel_id']}>" if panel else "未設置", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @verify.command(name="解除", description="認証設定を解除（パネルも削除を試みます）")
    async def disable_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._remove_old_panel(interaction.guild)
        for key in ("verify_role", "verify_min_age_days", "verify_contact", "verify_panel"):
            self.bot.store.delete_setting(interaction.guild.id, key)
        await interaction.followup.send(embed=h.ok_embed("解除しました", "認証設定を解除し、パネルを削除しました（削除できなかった場合は手動で消してください）。"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Verification(bot))
    bot.add_view(VerifyView(bot))
