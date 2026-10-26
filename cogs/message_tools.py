"""メッセージ・チャンネル管理: 一括削除 / 全消去(nuke) / スローモード / ロック / お知らせ / 代理発言。"""

import datetime

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h

# Discord の一括削除(bulk delete)が使える期間。14日ちょうどは境界で失敗しうるため余裕を持たせる
BULK_DELETE_DAYS = 13
# この件数以上の一括削除は、誤操作に備えて確認ボタンを挟む
BULK_CONFIRM_FROM = 20


class MessageTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # 一括削除・お知らせ・代理発言は「メッセージの管理」、全消去・スローモード・ロックは「チャンネルの管理」。
    # どちらか一方の権限でも入れるようにし、細かい権限は各コマンドで検査する
    manage = h.PermGroup(name="管理", description="🧹 メッセージ・チャンネルの整理と投稿（権限: メッセージ/チャンネルの管理）", any_of=("manage_messages", "manage_channels"))

    @manage.command(name="一括削除", description="メッセージをまとめて削除（最大1000件・13日以内）")
    @app_commands.rename(amount="件数", member="対象")
    @app_commands.describe(amount="削除する件数 1〜1000（対象を指定した場合は、直近この件数の中から削除）",
                           member="このメンバーの投稿だけ削除（省略時: 全員）")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def clear(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000], member: discord.Member = None):
        if amount >= BULK_CONFIRM_FROM:
            who = f"{member.mention} の投稿を含む" if member else "このチャンネルの"
            ok = await h.confirm_action(interaction, "まとめて削除しますか？",
                                        f"{who}直近 {amount} 件を削除します（元に戻せません）。", "削除する")
            if ok is not True:
                return
        else:
            await interaction.response.defer(ephemeral=True)
        # check=None を明示すると discord.py が None を呼び出して落ちるため、指定時のみ渡す
        kwargs = {"check": lambda m: m.author == member} if member else {}
        # 14日より古いメッセージは Discord の一括削除が使えず1件ずつ（約1件/秒）の削除になり、
        # 数百件で数分〜十数分固まる。bulk 対象の期間に限定し、新しい順に取る(after 指定時は古い順が既定)
        cutoff = discord.utils.utcnow() - datetime.timedelta(days=BULK_DELETE_DAYS)
        deleted = await interaction.channel.purge(limit=amount, after=cutoff, oldest_first=False, **kwargs)
        note = ""
        if len(deleted) < amount:
            # 少なかった理由が「古いメッセージが残っているから」のときだけ案内する
            older = [m async for m in interaction.channel.history(limit=1, before=cutoff)]
            if older:
                note = f"\n※ {BULK_DELETE_DAYS}日より古いメッセージは対象外です（`/管理 全消去` で消せます）。"
        await interaction.followup.send(embed=h.ok_embed("削除しました", f"{len(deleted)} 件のメッセージを削除しました。{note}"), ephemeral=True)
        e = h.embed("🧹 メッセージの一括削除", color=h.LOG)
        e.add_field(name="チャンネル", value=interaction.channel.mention, inline=True)
        e.add_field(name="件数", value=str(len(deleted)), inline=True)
        e.add_field(name="実行者", value=interaction.user.mention, inline=True)
        await h.send_log(self.bot, interaction.guild, "mod_log_channel", e)

    @manage.command(name="全消去", description="⚠ このチャンネルの全投稿を消去（複製して作り直し・元に戻せません）")
    @app_commands.rename(reason="理由")
    @app_commands.describe(reason="ログに残す理由（省略可）")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def nuke(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 400] = None):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(embed=h.error_embed("使えません", "テキストチャンネルでのみ使えます。"), ephemeral=True)

        ok = await h.confirm_action(
            interaction, "本当に全消去しますか？",
            f"{channel.mention} の**すべての投稿**が消えます（復元不可）。\n"
            "チャンネルは複製され、設定・権限・トピックは引き継がれますが、\n"
            "ピン留め・過去ログ・このチャンネルに紐づく認証パネルや抽選のメッセージは失われます。",
            "全消去する")
        if ok is not True:
            return

        audit = f"nuke by {interaction.user}: {reason or '理由なし'}"
        position = channel.position
        try:
            new_channel = await channel.clone(reason=audit)
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=h.error_embed("複製に失敗しました", f"チャンネルを複製できませんでした（コード {exc.status}）。Bot の権限を確認してください。"), ephemeral=True)
        try:
            await channel.delete(reason=audit)
        except discord.HTTPException as exc:
            leftover = ""
            try:
                await new_channel.delete(reason="nuke 失敗のため複製を破棄")  # 重複チャンネルを残さない
            except discord.HTTPException:
                leftover = f"\n複製した {new_channel.mention} も削除できませんでした。手動で削除してください。"
            return await interaction.followup.send(embed=h.error_embed("削除に失敗しました", f"元のチャンネルを削除できず、中止しました（コード {exc.status}）。{leftover}"), ephemeral=True)
        try:
            await new_channel.edit(position=position)
        except discord.HTTPException:
            pass  # 位置が戻らないだけなので続行

        self._migrate_channel_settings(interaction.guild.id, channel.id, new_channel.id)

        done = h.ok_embed("全消去しました", f"このチャンネルを作り直しました。\n実行者: {interaction.user.mention}")
        if reason:
            done.add_field(name="理由", value=reason, inline=False)
        await new_channel.send(embed=done)

        log = h.embed("💥 チャンネルの全消去", color=h.MOD)
        log.add_field(name="チャンネル", value=f"#{new_channel.name}", inline=True)
        log.add_field(name="実行者", value=interaction.user.mention, inline=True)
        if reason:
            log.add_field(name="理由", value=reason, inline=False)
        await h.send_log(self.bot, interaction.guild, "mod_log_channel", log)

    def _migrate_channel_settings(self, guild_id: int, old_id: int, new_id: int):
        """nuke でチャンネルIDが変わるため、旧IDを指す *_channel 設定と進行中の抽選の発表先を新IDへ付け替える。"""
        store = self.bot.store
        for key, value in store.all_settings(guild_id).items():
            if key.endswith("_channel") and value == old_id:
                store.set_setting(guild_id, key, new_id)
            elif key.startswith("giveaway_") and isinstance(value, dict) and value.get("channel_id") == old_id:
                value["channel_id"] = new_id  # 進行中の抽選の結果発表先
                store.set_setting(guild_id, key, value)

    @manage.command(name="スローモード", description="チャンネルの投稿間隔（スローモード）を設定")
    @app_commands.rename(seconds="秒数")
    @app_commands.describe(seconds="投稿間隔の秒数 0〜21600（0で解除）")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await interaction.response.send_message(embed=h.ok_embed("スローモードを解除しました", "このチャンネルのスローモードを解除しました。"))
        else:
            await interaction.response.send_message(embed=h.ok_embed("スローモードを設定しました", f"投稿間隔を {h.format_duration(seconds)} にしました。"))

    @manage.command(name="ロック", description="このチャンネルで @everyone の発言を禁止")
    @app_commands.rename(reason="理由")
    @app_commands.describe(reason="チャンネルに表示する理由（省略可）")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 400] = None):
        everyone = interaction.guild.default_role
        overwrite = interaction.channel.overwrites_for(everyone)
        if overwrite.send_messages is False:
            return await interaction.response.send_message(embed=h.warn_embed("すでにロック中です", "このチャンネルは @everyone の発言が禁止されています。"), ephemeral=True)
        # 解除時に元の状態（許可/未設定）へ戻せるよう、ロック前の値を保存する
        self.bot.store.set_setting(interaction.guild.id, f"lock_prev_{interaction.channel.id}", overwrite.send_messages)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(everyone, overwrite=overwrite, reason=f"{interaction.user}: {reason or '未指定'}")
        await interaction.response.send_message(embed=h.warn_embed("🔒 チャンネルをロックしました", reason or "このチャンネルは現在ロックされています。"))

    @manage.command(name="ロック解除", description="チャンネルのロックを解除")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        everyone = interaction.guild.default_role
        overwrite = interaction.channel.overwrites_for(everyone)
        if overwrite.send_messages is not False:
            return await interaction.response.send_message(embed=h.warn_embed("ロックされていません", "このチャンネルは @everyone の発言が禁止されていません。"), ephemeral=True)
        key = f"lock_prev_{interaction.channel.id}"
        overwrite.send_messages = self.bot.store.get_setting(interaction.guild.id, key, None)  # ロック前の値（未保存なら未設定）
        await interaction.channel.set_permissions(everyone, overwrite=overwrite)
        self.bot.store.delete_setting(interaction.guild.id, key)
        await interaction.response.send_message(embed=h.ok_embed("🔓 ロックを解除しました", "ロック前の状態に戻しました。"))

    @manage.command(name="お知らせ", description="埋め込み形式でお知らせを投稿")
    @app_commands.rename(channel="チャンネル", title="タイトル", message="本文", mention_everyone="全体メンション")
    @app_commands.describe(channel="投稿先のチャンネル", title="お知らせのタイトル", message="本文（\\n で改行）",
                           mention_everyone="ON にすると @everyone で通知（省略時: OFF。「@everyone へのメンション」権限が必要）")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, title: app_commands.Range[str, 1, 200],
                       message: app_commands.Range[str, 1, 3500], mention_everyone: bool = False):
        e = h.embed(f"📢 {title}", message.replace("\\n", "\n"), h.INFO)
        e.set_footer(text=f"投稿者: {interaction.user.display_name}")
        if not channel.permissions_for(interaction.user).send_messages:
            return await interaction.response.send_message(embed=h.error_embed("権限がありません", "あなたはそのチャンネルに投稿できません。"), ephemeral=True)
        if mention_everyone and not interaction.user.guild_permissions.mention_everyone:
            return await interaction.response.send_message(embed=h.error_embed("権限がありません", "@everyone を付けるには「@everyone へのメンション」権限が必要です。"), ephemeral=True)
        content = "@everyone" if mention_everyone else None
        await channel.send(content=content, embed=e, allowed_mentions=discord.AllowedMentions(everyone=mention_everyone))
        await interaction.response.send_message(embed=h.ok_embed("投稿しました", f"{channel.mention} にお知らせを投稿しました。"), ephemeral=True)
        await h.send_log(self.bot, interaction.guild, "mod_log_channel",
                         h.embed("📢 お知らせの投稿", f"{interaction.user.mention} が {channel.mention} に「{title}」を投稿しました。", h.LOG))

    @manage.command(name="代理発言", description="Bot に代わりにメッセージを送らせる（実行者はログに記録）")
    @app_commands.rename(message="本文", channel="チャンネル")
    @app_commands.describe(message="送信する内容（\\n で改行）", channel="送信先のチャンネル（省略時: このチャンネル）")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def say(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 1900], channel: discord.TextChannel = None):
        target = channel or interaction.channel
        if not target.permissions_for(interaction.user).send_messages:
            return await interaction.response.send_message(embed=h.error_embed("権限がありません", "あなたはそのチャンネルに投稿できません。"), ephemeral=True)
        text = message.replace("\\n", "\n")
        # 実行者に無い mention 権限を Bot 経由で行使させない
        await target.send(text, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message(embed=h.ok_embed("送信しました", f"{target.mention} に送信しました。"), ephemeral=True)
        # Bot の発言に見えるため、誰が言わせたかをモデログに残す
        log = h.embed("💬 代理発言", color=h.LOG)
        log.add_field(name="実行者", value=interaction.user.mention, inline=True)
        log.add_field(name="送信先", value=target.mention, inline=True)
        log.add_field(name="内容", value=text[:1000], inline=False)
        await h.send_log(self.bot, interaction.guild, "mod_log_channel", log)


async def setup(bot):
    await bot.add_cog(MessageTools(bot))
