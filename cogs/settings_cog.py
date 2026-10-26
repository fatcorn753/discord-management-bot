"""サーバー全体の基本設定（ログ送信先・警告しきい値）と、導入直後の設定診断。"""

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import helpers as h

# Bot が各機能を使うために必要な権限（管理者権限があれば全部満たす）
REQUIRED_BOT_PERMISSIONS = {
    "view_channel": "チャンネルを見る", "send_messages": "メッセージを送信", "embed_links": "埋め込みリンク",
    "read_message_history": "メッセージ履歴を読む", "add_reactions": "リアクションの追加",
    "manage_messages": "メッセージの管理", "manage_channels": "チャンネルの管理", "manage_roles": "ロールの管理",
    "kick_members": "メンバーをキック", "ban_members": "メンバーをBAN", "moderate_members": "メンバーをタイムアウト",
}

RETENTION_DAYS = 30  # Bot がサーバーから外れた後、そのサーバーのデータを残す日数
OK, WARN, INFO = "✅", "⚠️", "ℹ️"


def purge_departed(store, current_guild_ids: set, now: float) -> list:
    """Bot がいなくなったサーバーのデータを管理する。削除したサーバーIDを返す。

    - データはあるのに Bot がいないサーバーは、退出日時を記録する（停止中に外された場合も拾う）
    - 退出から RETENTION_DAYS 日たったら削除する
    - 期間内に再招待された場合は、記録を消してデータをそのまま使う
    """
    left = store.left_guilds()
    for gid in store.known_guild_ids():
        if gid not in current_guild_ids and gid not in left:
            store.mark_guild_left(gid, now)
            left[gid] = now
    purged = []
    for gid, when in left.items():
        if gid in current_guild_ids:
            store.clear_guild_left(gid)
        elif now - when >= RETENTION_DAYS * 86400:
            store.delete_guild_data(gid)
            store.clear_guild_left(gid)
            purged.append(gid)
    return purged


def run_diagnosis(guild: discord.Guild, store) -> list:
    """導入直後に確認したい設定を調べ、[(記号, 説明), ...] を返す。"""
    gid = guild.id
    me = guild.me
    out = []

    # 1) Bot の権限
    if me.guild_permissions.administrator:
        out.append((OK, "Bot の権限: 管理者権限があります。"))
    else:
        missing = [label for key, label in REQUIRED_BOT_PERMISSIONS.items() if not getattr(me.guild_permissions, key)]
        if missing:
            out.append((WARN, f"Bot の権限が足りません: {'、'.join(missing)}。招待し直すか、Bot のロールに権限を足してください。"))
        else:
            out.append((OK, "Bot の権限: 必要な権限がそろっています。"))

    # 2) Bot のロール順位（ロールの付与・処罰は、Bot より下のロールにしかできない）
    above = [r for r in guild.roles if r > me.top_role]
    if above:
        out.append((INFO, f"Bot のロールより上に {len(above)} 個のロールがあります。認証ロールなど、Bot が付与するロールは Bot より下にしてください。"))
    else:
        out.append((OK, "Bot のロールは最上位です。"))

    # 3) ログの送信先
    for key, label in (("log_channel", "ログ"), ("mod_log_channel", "モデログ"), ("automod_log_channel", "自動対策ログ")):
        cid = store.get_setting(gid, key)
        ch = guild.get_channel(int(cid)) if cid else None
        if not cid:
            out.append((WARN, f"{label}の送信先が未設定です。`/設定 {label}` で設定してください（未設定だと記録が残りません）。"))
        elif ch is None:
            out.append((WARN, f"{label}の送信先チャンネルが見つかりません（削除された可能性）。`/設定 {label}` で設定し直してください。"))
        elif not ch.permissions_for(me).send_messages:
            out.append((WARN, f"{ch.mention} に Bot が送信できません（{label}）。"))
        else:
            out.append((OK, f"{label}: {ch.mention}"))

    # 4) 自動対策
    if store.get_setting(gid, "automod_enabled", False):
        out.append((OK, "自動対策: ON"))
    else:
        out.append((INFO, "自動対策は OFF です。使う場合は `/自動対策 全体 有効:True` を実行します。"))

    # 5) 認証ゲート / 自動ロール
    verify_id = store.get_setting(gid, "verify_role")
    if verify_id:
        role = guild.get_role(int(verify_id))
        if role is None:
            out.append((WARN, "認証ロールが見つかりません。`/認証 設置` をやり直してください。"))
        elif role >= me.top_role:
            out.append((WARN, f"認証ロール {role.mention} が Bot のロールより上です。付与できないため、Bot のロールを上へ動かしてください。"))
        elif not (panel := store.get_setting(gid, "verify_panel")):
            out.append((WARN, "認証パネルが未設置です。`/認証 設置` で設置してください。"))
        else:
            out.append((OK, f"認証ゲート: {role.mention}"))
            # 認証ゲートの要: 認証前の人（@everyone）が、認証チャンネル以外を見られないこと
            allowed = {int(panel["channel_id"])}
            if store.get_setting(gid, "verify_contact"):
                allowed.add(int(store.get_setting(gid, "verify_contact")))
            exposed = [c for c in getattr(guild, "channels", [])
                       if getattr(c, "category_id", 0) is not None and c.id not in allowed
                       and not isinstance(c, discord.CategoryChannel)
                       and c.permissions_for(guild.default_role).view_channel]
            if exposed:
                names = "、".join(f"#{c.name}" for c in exposed[:5])
                out.append((WARN, f"認証前の人（@everyone）が見られるチャンネルが {len(exposed)} 個あります（{names}{'…' if len(exposed) > 5 else ''}）。認証ゲートの意味がなくなるため、@everyone の「チャンネルを見る」を外してください。"))
    else:
        out.append((INFO, "認証ゲートは未設定です（荒らし対策に使う場合は `/認証 設置`）。"))
    auto_id = store.get_setting(gid, "autorole")
    if auto_id:
        role = guild.get_role(int(auto_id))
        if role is None or role >= me.top_role:
            out.append((WARN, "自動ロールが見つからないか、Bot のロールより上です。`/入退室 自動ロール` を確認してください。"))

    # 6) 警告
    if not store.get_setting(gid, "warn_threshold", 0):
        out.append((INFO, "警告の自動処罰は無効です。使う場合は `/設定 警告しきい値` で設定します。"))
    return out


class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.purge_loop.start()

    def cog_unload(self):
        self.purge_loop.cancel()

    @tasks.loop(hours=6)
    async def purge_loop(self):
        purged = purge_departed(self.bot.store, {g.id for g in self.bot.guilds}, time.time())
        if purged:
            print(f"退出から{RETENTION_DAYS}日たったサーバーのデータを削除しました: {purged}")

    @purge_loop.before_loop
    async def _before_purge(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """導入直後に、最初にやることを案内する。"""
        self.bot.store.clear_guild_left(guild.id)  # 期間内の再招待なら、以前の設定がそのまま使える
        e = h.embed("👋 導入ありがとうございます",
                    "管理補助bot です。まず、管理者の方が次の3つを行ってください。\n\n"
                    "1️⃣ **ロールの順位を直す**\n"
                    "サーバー設定 → ロール で「管理補助bot」を、認証ロールなど付与したいロールより**上**にドラッグします。"
                    "（Bot は自分より下のロールしか付与・処罰できません）\n\n"
                    "2️⃣ **`/設定 診断` を実行する**\n"
                    "権限・ロール順位・ログの送信先をまとめてチェックし、直すべき点を示します。\n\n"
                    "3️⃣ **ログの送信先を決める**\n"
                    "`/設定 ログ チャンネル:#ログ`（未設定だと記録が残りません）\n\n"
                    "コマンド一覧は `/ヘルプ` で見られます。", h.INFO)
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        if channel is not None:
            try:
                await channel.send(embed=e)
                return
            except discord.HTTPException:
                pass
        # どのチャンネルにも書けなかったときは、サーバーの所有者へDMで知らせる（DMを閉じていれば諦める）
        try:
            owner = guild.owner or (await self.bot.fetch_user(guild.owner_id) if guild.owner_id else None)
            if owner is not None:
                await owner.send(embed=e)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Bot が外れたら退出日時を記録する。データは RETENTION_DAYS 日後に削除（それまでは再招待で復活）。"""
        self.bot.store.mark_guild_left(guild.id)

    setting = h.PermGroup(name="設定", description="🔧 ログ送信先・警告しきい値・導入チェック（権限: サーバー管理）", required={"manage_guild": True})

    @setting.command(name="診断", description="導入直後の設定チェック（権限・ログ先・ロール順位）")
    async def diagnose(self, interaction: discord.Interaction):
        results = run_diagnosis(interaction.guild, self.bot.store)
        problems = sum(1 for mark, _ in results if mark == WARN)
        title = "🔧 設定診断" if problems == 0 else f"🔧 設定診断（要確認 {problems} 件）"
        e = h.embed(title, "\n".join(f"{mark} {text}" for mark, text in results)[:4000], h.OK if problems == 0 else h.WARN)
        e.set_footer(text="⚠️ は対応をおすすめします / ℹ️ は必要に応じて")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @setting.command(name="ログ", description="一般イベントログ（編集・削除・入退室など）の送信先を設定")
    @app_commands.rename(channel="チャンネル")
    @app_commands.describe(channel="ログを送るチャンネル")
    async def logchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._set_log(interaction, "log_channel", channel, "ログ")

    @setting.command(name="モデログ", description="モデレーション操作ログの送信先を設定")
    @app_commands.rename(channel="チャンネル")
    @app_commands.describe(channel="処罰・予約失敗などの記録を送るチャンネル")
    async def modlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._set_log(interaction, "mod_log_channel", channel, "モデログ")

    @setting.command(name="自動対策ログ", description="自動対策の発動ログの送信先を設定")
    @app_commands.rename(channel="チャンネル")
    @app_commands.describe(channel="自動削除・連投検知などの記録を送るチャンネル")
    async def automodlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._set_log(interaction, "automod_log_channel", channel, "自動対策ログ")

    async def _set_log(self, interaction: discord.Interaction, key: str, channel: discord.TextChannel, label: str):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                embed=h.error_embed("設定できません", f"Bot が {channel.mention} に埋め込みを送信できません。チャンネルの権限を確認してください。"), ephemeral=True)
        self.bot.store.set_setting(interaction.guild.id, key, channel.id)
        await interaction.response.send_message(embed=h.ok_embed("設定しました", f"{label}を {channel.mention} に送ります。"), ephemeral=True)

    @setting.command(name="警告しきい値", description="警告が何件で自動処罰するかを設定")
    @app_commands.rename(count="警告数", action="処罰")
    @app_commands.describe(count="自動処罰が働く警告の件数 0〜20（0で無効）", action="到達時の処罰（省略時: 変更なし。初期値はタイムアウト）")
    @app_commands.choices(action=[
        app_commands.Choice(name="タイムアウト（1時間）", value="timeout"),
        app_commands.Choice(name="キック", value="kick"),
        app_commands.Choice(name="BAN", value="ban"),
    ])
    async def warnthreshold(self, interaction: discord.Interaction, count: app_commands.Range[int, 0, 20], action: app_commands.Choice[str] = None):
        self.bot.store.set_setting(interaction.guild.id, "warn_threshold", count)
        if action:
            self.bot.store.set_setting(interaction.guild.id, "warn_action", action.value)
        act = action.value if action else self.bot.store.get_setting(interaction.guild.id, "warn_action", "timeout")
        label = h.WARN_ACTION_JP.get(act, act)
        e = h.ok_embed("警告の自動処罰", "無効にしました。" if count == 0 else f"警告が {count} 件になると、自動で{label}を実行します。")
        if count and act in ("kick", "ban"):
            e.add_field(name="⚠️ ご注意", value=f"警告が {count} 件に達した人は、確認なしで自動的に{label}されます。誤った警告に注意してください。", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @setting.command(name="状況", description="現在の主要設定をまとめて表示")
    async def view(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        s = self.bot.store

        def ch(key):
            cid = s.get_setting(gid, key)
            return f"<#{cid}>" if cid else "未設定"

        def role(key):
            rid = s.get_setting(gid, key)
            return f"<@&{rid}>" if rid else "未設定"

        e = h.embed("🔧 サーバー設定の状況", "詳しい確認は `/設定 診断` で行えます。", h.INFO)
        e.add_field(name="ログ", value=ch("log_channel"), inline=True)
        e.add_field(name="モデログ", value=ch("mod_log_channel"), inline=True)
        e.add_field(name="自動対策ログ", value=ch("automod_log_channel"), inline=True)
        e.add_field(name="参加メッセージ", value=ch("welcome_channel"), inline=True)
        e.add_field(name="退出メッセージ", value=ch("goodbye_channel"), inline=True)
        e.add_field(name="自動ロール", value=role("autorole"), inline=True)
        e.add_field(name="認証ロール", value=role("verify_role"), inline=True)
        e.add_field(name="チケットのスタッフ", value=role("ticket_staff_role"), inline=True)
        e.add_field(name="チケットのログ", value=ch("ticket_log_channel"), inline=True)
        threshold = s.get_setting(gid, "warn_threshold", 0)
        e.add_field(name="警告の自動処罰",
                    value=f"{threshold}件で{h.WARN_ACTION_JP.get(s.get_setting(gid, 'warn_action', 'timeout'))}" if threshold else "無効", inline=True)
        e.add_field(name="自動対策", value="ON" if s.get_setting(gid, "automod_enabled") else "OFF（`/自動対策 状況`）", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Settings(bot))
