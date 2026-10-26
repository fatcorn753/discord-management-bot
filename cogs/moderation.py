"""モデレーション: キック / BAN / BAN解除（トップレベル）と、タイムアウト / 警告（/処罰）。

権限の表示制御（default_permissions）は複数権限が AND になり、グループ単位にしか付けられない。
権限が違う操作（キック・BAN・タイムアウト）を1グループに入れると誰にも正しく表示できないため、
キック・バン・バン解除はトップレベルのコマンドに分け、/処罰 はタイムアウト権限で揃うものだけにしている。
"""

import datetime

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h

CONTACT_NOTE = "\n\n誤りだと思う場合は、サーバーのスタッフへ連絡してください。"
WARN_LIST_LIMIT = 10  # 1件ごとに理由を載せるため、埋め込みの文字数上限(6000)に収まる件数


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ #
    @staticmethod
    def _audit_reason(moderator, reason):
        return f"{moderator}: {reason or '未指定'}"

    @staticmethod
    async def _retract_dm(member):
        """処罰の事前DMを送った後に処罰が失敗した場合の訂正通知。"""
        try:
            await member.send(embed=h.embed("訂正", "先ほどの処罰通知は、実行に失敗したため無効です。", h.INFO))
        except discord.HTTPException:
            pass

    async def _log_action(self, guild, action, target, moderator, reason, extra=""):
        e = h.embed(f"🔨 {action}", color=h.MOD)
        e.add_field(name="対象", value=f"{target} (`{target.id}`)", inline=False)
        e.add_field(name="実行者", value=moderator.mention, inline=True)
        if extra:
            e.add_field(name="詳細", value=extra, inline=True)
        e.add_field(name="理由", value=reason or "未指定", inline=False)
        if isinstance(target, (discord.Member, discord.User)) and target.display_avatar:
            e.set_thumbnail(url=target.display_avatar.url)
        await h.send_log(self.bot, guild, "mod_log_channel", e)

    # --------------------------- キック / BAN --------------------------- #
    @app_commands.command(name="キック", description="👢 メンバーをサーバーからキック（権限: キック）")
    @app_commands.rename(member="対象", reason="理由")
    @app_commands.describe(member="キックするメンバー", reason="監査ログとDMに残す理由（省略可）")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 400] = None):
        err = h.can_target(interaction.user, member)
        if err:
            return await interaction.response.send_message(embed=h.error_embed("実行できません", err), ephemeral=True)
        try:
            await member.send(embed=h.warn_embed("キックされました", f"サーバー: {interaction.guild.name}\n理由: {reason or '未指定'}{CONTACT_NOTE}"))
        except discord.HTTPException:
            pass
        try:
            await member.kick(reason=self._audit_reason(interaction.user, reason))
        except discord.HTTPException as exc:
            await self._retract_dm(member)
            return await interaction.response.send_message(embed=h.error_embed("キックに失敗しました", f"Bot の権限とロール順位を確認してください（コード {exc.status}）。"), ephemeral=True)
        await interaction.response.send_message(embed=h.ok_embed("キックしました", f"{member.mention} をキックしました。"))
        await self._log_action(interaction.guild, "キック", member, interaction.user, reason)

    @app_commands.command(name="バン", description="🔨 メンバーをBAN（権限: BAN）。退出済みの人はID指定")
    @app_commands.rename(member="対象", user_id="ユーザーid", reason="理由", delete_days="メッセージ削除日数")
    @app_commands.describe(
        member="BANするメンバー（退出済みの人は省略して「ユーザーid」を使う）",
        user_id="BANするユーザーのID（数字のみ。退出済みの人用）",
        reason="監査ログとDMに残す理由（省略可）",
        delete_days="過去何日分の投稿を消すか 0〜7（省略時: 0）")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member = None, user_id: str = None,
                  reason: app_commands.Range[str, 1, 400] = None, delete_days: app_commands.Range[int, 0, 7] = 0):
        guild = interaction.guild
        if (member is None) == (user_id is None):
            return await interaction.response.send_message(
                embed=h.error_embed("入力エラー", "「対象」か「ユーザーid」のどちらか一方を指定してください。"), ephemeral=True)

        if member is None:
            if not h.is_id(user_id):
                return await interaction.response.send_message(embed=h.error_embed("入力エラー", "ユーザーidは数字だけで入力してください。"), ephemeral=True)
            uid = int(user_id)
            member = guild.get_member(uid)  # 実は在籍中なら、通常のBANと同じ検査を通す
            target = member
            if member is None:
                if uid in (interaction.user.id, guild.owner_id, self.bot.user.id):
                    return await interaction.response.send_message(embed=h.error_embed("実行できません", "そのユーザーはBANできません。"), ephemeral=True)
                try:
                    target = await self.bot.fetch_user(uid)
                except discord.NotFound:
                    return await interaction.response.send_message(embed=h.error_embed("見つかりません", "そのIDのユーザーは存在しません。IDを確認してください。"), ephemeral=True)
        else:
            target = member

        if member is not None:
            err = h.can_target(interaction.user, member)
            if err:
                return await interaction.response.send_message(embed=h.error_embed("実行できません", err), ephemeral=True)

        detail = f"{target} (`{target.id}`) を BAN します。\nメッセージ削除: 過去 {delete_days} 日分\n理由: {reason or '未指定'}"
        if await h.confirm_action(interaction, "BANしますか？", detail, "BANする") is not True:
            return

        if member is not None:
            try:
                await member.send(embed=h.error_embed("BANされました", f"サーバー: {guild.name}\n理由: {reason or '未指定'}{CONTACT_NOTE}"))
            except discord.HTTPException:
                pass
        try:
            await guild.ban(target, reason=self._audit_reason(interaction.user, reason), delete_message_seconds=delete_days * 86400)
        except discord.HTTPException as exc:
            if member is not None:
                await self._retract_dm(member)
            return await interaction.followup.send(embed=h.error_embed("BANに失敗しました", f"Bot の権限とロール順位を確認してください（コード {exc.status}）。"), ephemeral=True)
        await interaction.followup.send(embed=h.ok_embed("BANしました", f"{target.mention if member else target} をBANしました。"))
        await self._log_action(guild, "BAN", target, interaction.user, reason)

    async def _banned_autocomplete(self, interaction: discord.Interaction, current: str):
        """BAN中のユーザーを候補に出す（IDの手入力を不要にする）。"""
        if not interaction.permissions.ban_members:
            return []  # コマンドの表示設定が上書きされていても、BAN一覧を見せない
        try:
            entries = [e async for e in interaction.guild.bans(limit=200)]
        except discord.HTTPException:
            return []
        current = current.lower()
        found = [e.user for e in entries if current in str(e.user).lower() or current in str(e.user.id)]
        return [app_commands.Choice(name=f"{u} ({u.id})"[:100], value=str(u.id)) for u in found[:25]]

    @app_commands.command(name="バン解除", description="🔓 BANを解除（権限: BAN）")
    @app_commands.rename(user_id="ユーザーid", reason="理由")
    @app_commands.describe(user_id="解除するユーザー（候補から選ぶか、IDを入力）", reason="監査ログに残す理由（省略可）")
    @app_commands.autocomplete(user_id=_banned_autocomplete)
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: app_commands.Range[str, 1, 400] = None):
        if not h.is_id(user_id):
            return await interaction.response.send_message(embed=h.error_embed("入力エラー", "ユーザーidは数字だけで入力してください。"), ephemeral=True)
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=self._audit_reason(interaction.user, reason))
        except discord.NotFound:
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", "そのユーザーはBANされていません。"), ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=h.error_embed("解除できませんでした", f"IDを確認してください（コード {exc.status}）。"), ephemeral=True)
        await interaction.response.send_message(embed=h.ok_embed("BANを解除しました", f"{user} のBANを解除しました。"))
        await self._log_action(interaction.guild, "BAN解除", user, interaction.user, reason)

    # ------------------------- /処罰（タイムアウト・警告） ------------------------- #
    punish = h.PermGroup(name="処罰", description="🔨 タイムアウトと警告の管理（権限: メンバーをタイムアウト）", required={"moderate_members": True})

    @punish.command(name="タイムアウト", description="メンバーを一定時間タイムアウト（発言禁止）")
    @app_commands.rename(member="対象", duration="期間", reason="理由")
    @app_commands.describe(member="タイムアウトするメンバー", duration="期間 例: 10m, 1h, 2d（最大28日）", reason="監査ログとDMに残す理由（省略可）")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: app_commands.Range[str, 1, 400] = None):
        err = h.can_target(interaction.user, member)
        if err:
            return await interaction.response.send_message(embed=h.error_embed("実行できません", err), ephemeral=True)
        seconds = h.parse_duration(duration)
        if seconds is None or seconds <= 0:
            return await interaction.response.send_message(embed=h.error_embed("期間を読み取れません", "例: `10m` `1h30m` `2d`（最大28日）"), ephemeral=True)
        if seconds > 28 * 86400:
            return await interaction.response.send_message(embed=h.error_embed("期間が長すぎます", "タイムアウトは最大28日までです。"), ephemeral=True)
        until = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
        await member.timeout(until, reason=self._audit_reason(interaction.user, reason))
        try:
            await member.send(embed=h.warn_embed("タイムアウトされました", f"サーバー: {interaction.guild.name}\n期間: {h.format_duration(seconds)}\n理由: {reason or '未指定'}{CONTACT_NOTE}"))
        except discord.HTTPException:
            pass
        await interaction.response.send_message(embed=h.ok_embed("タイムアウトしました", f"{member.mention} を {h.format_duration(seconds)} タイムアウトしました。"))
        await self._log_action(interaction.guild, "タイムアウト", member, interaction.user, reason, extra=h.format_duration(seconds))

    @punish.command(name="タイムアウト解除", description="タイムアウトを解除")
    @app_commands.rename(member="対象", reason="理由")
    @app_commands.describe(member="解除するメンバー", reason="監査ログに残す理由（省略可）")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 400] = None):
        err = h.can_target(interaction.user, member)
        if err:
            return await interaction.response.send_message(embed=h.error_embed("実行できません", err), ephemeral=True)
        if member.timed_out_until is None:
            return await interaction.response.send_message(embed=h.error_embed("対象外です", "そのメンバーはタイムアウトされていません。"), ephemeral=True)
        await member.timeout(None, reason=self._audit_reason(interaction.user, reason))
        await interaction.response.send_message(embed=h.ok_embed("解除しました", f"{member.mention} のタイムアウトを解除しました。"))
        await self._log_action(interaction.guild, "タイムアウト解除", member, interaction.user, reason)

    @punish.command(name="警告付与", description="メンバーに警告を付与（しきい値で自動処罰）")
    @app_commands.rename(member="対象", reason="理由")
    @app_commands.describe(member="警告するメンバー", reason="警告の理由（本人にDMで伝わります）")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_add(self, interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 500]):
        err = h.can_target(interaction.user, member)
        if err:
            return await interaction.response.send_message(embed=h.error_embed("実行できません", err), ephemeral=True)
        wid = self.bot.store.add_warning(interaction.guild.id, member.id, interaction.user.id, reason)
        count = len(self.bot.store.get_warnings(interaction.guild.id, member.id))
        try:
            await member.send(embed=h.warn_embed("警告を受けました", f"サーバー: {interaction.guild.name}\n理由: {reason}\n累計警告数: {count}{CONTACT_NOTE}"))
        except discord.HTTPException:
            pass
        await interaction.response.send_message(embed=h.ok_embed("警告を付与しました", f"{member.mention} に警告 #{wid} を付与しました（累計 {count} 件）。"))
        await self._log_action(interaction.guild, "警告", member, interaction.user, reason, extra=f"累計 {count} 件")

        # 自動処罰（しきい値はギルド設定 warn_threshold / warn_action）
        threshold = self.bot.store.get_setting(interaction.guild.id, "warn_threshold")
        action = self.bot.store.get_setting(interaction.guild.id, "warn_action", "timeout")
        if threshold and count >= int(threshold):
            label = h.WARN_ACTION_JP.get(action, action)
            if action in ("kick", "ban"):
                # 処罰の後は届かなくなるため、事前に伝える
                try:
                    await member.send(embed=h.warn_embed("警告が上限に達しました", f"サーバー: {interaction.guild.name}\n警告が {count} 件に達したため、自動的に{label}されます。{CONTACT_NOTE}"))
                except discord.HTTPException:
                    pass
            try:
                if action == "kick":
                    await member.kick(reason=f"警告{count}件到達による自動キック")
                elif action == "ban":
                    await member.ban(reason=f"警告{count}件到達による自動BAN")
                else:
                    until = discord.utils.utcnow() + datetime.timedelta(hours=1)
                    await member.timeout(until, reason=f"警告{count}件到達による自動タイムアウト")
                await interaction.followup.send(embed=h.warn_embed("自動処罰を実行しました", f"{member.mention} が警告しきい値（{threshold}件）に達したため、{label}を実行しました。"))
                await self._log_action(interaction.guild, f"自動処罰（{label}）", member, interaction.user, f"警告が {count} 件に達した", extra=f"しきい値 {threshold} 件")
            except discord.HTTPException as exc:
                msg = f"{member.mention} の自動処罰（{label}）に失敗しました。Bot の権限とロール順位を確認してください（コード {exc.status}）。"
                await interaction.followup.send(embed=h.error_embed("自動処罰に失敗しました", msg))
                await h.send_log(self.bot, interaction.guild, "mod_log_channel", h.error_embed("自動処罰に失敗しました", msg))

    @punish.command(name="警告一覧", description="メンバーの警告履歴を表示")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="履歴を見るメンバー")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_list(self, interaction: discord.Interaction, member: discord.Member):
        warns = self.bot.store.get_warnings(interaction.guild.id, member.id)
        if not warns:
            return await interaction.response.send_message(embed=h.embed("警告履歴", f"{member.mention} に警告はありません。", h.OK), ephemeral=True)
        note = f"（最新 {WARN_LIST_LIMIT} 件を表示）" if len(warns) > WARN_LIST_LIMIT else ""
        e = h.embed(f"⚠️ {member.display_name} の警告履歴", f"累計 {len(warns)} 件{note}", h.WARN)
        for w in warns[-WARN_LIST_LIMIT:]:
            ts = datetime.datetime.fromtimestamp(w["created_at"]).strftime("%Y-%m-%d %H:%M")
            e.add_field(name=f"#{w['id']} — {ts}", value=f"理由: {(w['reason'] or '未指定')[:120]}\n実行者: <@{w['moderator_id']}>", inline=False)
        e.set_footer(text="1件だけ消すには /処罰 警告削除 に警告の番号（#の数字）を指定します")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @punish.command(name="警告削除", description="警告を1件削除")
    @app_commands.rename(warning_id="警告id")
    @app_commands.describe(warning_id="削除する警告のID（警告一覧の #の数字）")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_remove(self, interaction: discord.Interaction, warning_id: int):
        ok = self.bot.store.remove_warning(warning_id, interaction.guild.id)
        if not ok:
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", f"警告 #{warning_id} は存在しません。"), ephemeral=True)
        await interaction.response.send_message(embed=h.ok_embed("削除しました", f"警告 #{warning_id} を削除しました。"), ephemeral=True)
        await h.send_log(self.bot, interaction.guild, "mod_log_channel",
                         h.embed("🔨 警告の削除", f"{interaction.user.mention} が警告 #{warning_id} を削除しました。", h.MOD))

    @punish.command(name="警告全消去", description="⚠ メンバーの警告をすべて消去（元に戻せません）")
    @app_commands.rename(member="対象")
    @app_commands.describe(member="警告を全消去するメンバー")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_clear(self, interaction: discord.Interaction, member: discord.Member):
        n = len(self.bot.store.get_warnings(interaction.guild.id, member.id))
        if n == 0:
            return await interaction.response.send_message(embed=h.embed("対象なし", f"{member.mention} に警告はありません。", h.INFO), ephemeral=True)
        if await h.confirm_action(interaction, "警告を全消去しますか？", f"{member.mention} の警告 {n} 件をすべて消去します（元に戻せません）。", "全消去する") is not True:
            return
        n = self.bot.store.clear_warnings(interaction.guild.id, member.id)
        await interaction.followup.send(embed=h.ok_embed("全消去しました", f"{member.mention} の警告 {n} 件を消去しました。"), ephemeral=True)
        await h.send_log(self.bot, interaction.guild, "mod_log_channel",
                         h.embed("🔨 警告の全消去", f"{interaction.user.mention} が {member.mention} の警告 {n} 件を消去しました。", h.MOD))


async def setup(bot):
    await bot.add_cog(Moderation(bot))
