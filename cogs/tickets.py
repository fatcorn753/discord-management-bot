"""チケット（サポート窓口）システム。

/チケット パネル で設置したパネルの「チケット作成」ボタンから
専用プライベートチャンネルを作成。スタッフと本人のみ閲覧可。
「クローズ」ボタン（または /チケット クローズ）は確認を挟み、会話を記録としてログチャンネルへ送ってから
チャンネルを削除する。ボタンは Bot 再起動後も有効（永続View）。

ギルド設定:
  ticket_category  : int   チケットチャンネルを作るカテゴリID（任意）
  ticket_staff_role: int   閲覧・クローズできるスタッフロールID（任意）
  ticket_log_channel: int  クローズ時の記録の送信先（任意）
  ticket_seq       : int   チケット番号の連番
"""

import asyncio
import datetime
import io

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h

MAX_OPEN_PER_USER = 3
TRANSCRIPT_MAX_MESSAGES = 1000
TRANSCRIPT_MAX_BYTES = 7_000_000  # Discord の添付上限(8MB)に収める


def build_transcript(channel_name: str, messages: list) -> str:
    """チケットの会話を、人が読めるテキストにする。"""
    lines = [f"# {channel_name} の記録", ""]
    for m in messages:
        ts = m.created_at.astimezone(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
        body = m.content or ""
        for a in m.attachments:
            body += f"\n  [添付] {a.filename} {a.url}"
        for e in m.embeds:
            if e.title or e.description:
                body += f"\n  [埋め込み] {e.title or ''} {e.description or ''}".rstrip()
        lines.append(f"[{ts}] {m.author} ({m.author.id}): {body}")
    return "\n".join(lines) + "\n"


def is_ticket_staff(store, member: discord.Member) -> bool:
    """チャンネル管理権限か、設定したスタッフロールを持つ人。"""
    if member.guild_permissions.manage_channels:
        return True
    role_id = store.get_setting(member.guild.id, "ticket_staff_role")
    return bool(role_id) and any(r.id == int(role_id) for r in member.roles)


async def close_ticket(bot, interaction: discord.Interaction, reason: str = None):
    """チケットのクローズ（確認 → 記録の送信 → 削除）。ボタンと /チケット クローズ で共有する。"""
    store = bot.store
    channel = interaction.channel
    ticket = store.get_ticket(channel.id)
    if ticket is None:
        return await interaction.response.send_message(embed=h.error_embed("使えません", "ここはチケットのチャンネルではありません。"), ephemeral=True)
    if not (interaction.user.id == ticket["user_id"] or is_ticket_staff(store, interaction.user)):
        return await interaction.response.send_message(embed=h.error_embed("権限がありません", "チケットを作った本人かスタッフだけがクローズできます。"), ephemeral=True)

    log_id = store.get_setting(interaction.guild.id, "ticket_log_channel")
    log_channel = interaction.guild.get_channel(int(log_id)) if log_id else None
    keep = ("会話の記録はログチャンネルに送られます。" if log_channel is not None
            else "ログチャンネルが未設定（または見つからない）ため、会話の記録は残りません。")
    ok = await h.confirm_action(interaction, "チケットをクローズしますか？",
                                f"このチャンネルは数秒後に削除されます。{keep}", "クローズする")
    if ok is not True:
        return

    # 記録を先に作って保存する（削除後は読めない）。保存できなければ削除せず中止する
    messages = [m async for m in channel.history(limit=TRANSCRIPT_MAX_MESSAGES, oldest_first=True)]
    text = build_transcript(channel.name, messages)
    data = text.encode("utf-8")[:TRANSCRIPT_MAX_BYTES]

    e = h.embed("🎫 チケットをクローズしました", color=h.LOG)
    e.add_field(name="チャンネル", value=f"#{channel.name}", inline=True)
    e.add_field(name="作成者", value=f"<@{ticket['user_id']}>", inline=True)
    e.add_field(name="クローズした人", value=interaction.user.mention, inline=True)
    if reason:
        e.add_field(name="理由", value=reason, inline=False)
    e.add_field(name="メッセージ数", value=str(len(messages)), inline=True)
    if log_channel is not None:
        try:
            await log_channel.send(embed=e, file=discord.File(io.BytesIO(data), filename=f"{channel.name}.txt"))
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=h.error_embed(
                "クローズを中止しました",
                f"会話の記録を {log_channel.mention} に送れなかったため、チャンネルは削除していません（コード {exc.status}）。"
                "Bot の権限（ファイルの添付）を確認してから、もう一度お試しください。"), ephemeral=True)

    await interaction.followup.send(embed=h.warn_embed("クローズします", "5秒後にこのチャンネルを削除します…"))
    await asyncio.sleep(5)
    try:
        await channel.delete(reason="チケットクローズ")
    except discord.HTTPException as exc:
        # 削除に失敗したら記録を残す（成功時は on_guild_channel_delete が記録を消す）
        msg = f"チャンネルを削除できませんでした。手動で削除してください（コード {exc.status}）。"
        await interaction.followup.send(embed=h.error_embed("クローズに失敗しました", msg))
        await h.send_log(bot, interaction.guild, "ticket_log_channel", h.error_embed("チケットを削除できませんでした", f"{channel.mention}: {msg}"))
        return
    store.remove_ticket(channel.id)


class TicketPanel(discord.ui.View):
    """パネル側「チケット作成」ボタン（永続）。"""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🎫 チケット作成", style=discord.ButtonStyle.primary, custom_id="ticket:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        store = self.bot.store

        # 既に開いているチケットが多すぎないか。どれを閉じればよいか分かるようにチャンネルを示す
        mine = [t for t in store.list_tickets(guild.id, interaction.user.id) if guild.get_channel(t["channel_id"])]
        if len(mine) >= MAX_OPEN_PER_USER:
            listing = " ".join(f"<#{t['channel_id']}>" for t in mine)
            return await interaction.response.send_message(
                embed=h.error_embed("チケットが上限です", f"同時に開けるチケットは {MAX_OPEN_PER_USER} 件までです。\n開いているチケット: {listing}\n解決したものはクローズしてください。"), ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        staff_role_id = store.get_setting(guild.id, "ticket_staff_role")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if staff_role_id:
            role = guild.get_role(int(staff_role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        category = None
        cat_id = store.get_setting(guild.id, "ticket_category")
        if cat_id:
            category = guild.get_channel(int(cat_id))

        seq = int(store.get_setting(guild.id, "ticket_seq", 0) or 0) + 1
        store.set_setting(guild.id, "ticket_seq", seq)
        try:
            channel = await guild.create_text_channel(
                name=f"チケット-{seq:04d}-{interaction.user.name}"[:90],
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                reason=f"チケット作成: {interaction.user}",
            )
        except discord.HTTPException:
            return await interaction.followup.send(embed=h.error_embed("作成できませんでした", "チャンネルを作成できませんでした。サーバーの管理者に、Bot の「チャンネルの管理」権限を確認してもらってください。"), ephemeral=True)

        store.add_ticket(channel.id, guild.id, interaction.user.id)
        e = h.embed(
            "🎫 サポートチケット",
            f"{interaction.user.mention} さん、お問い合わせ内容をこちらに書いてください。スタッフが対応します。\n"
            "解決したら、下の「🔒 クローズ」を押してください。\n"
            + ("※ この会話は、クローズ時に記録として運営が保管します。" if store.get_setting(guild.id, "ticket_log_channel") else ""), h.INFO)
        mention = f"<@&{staff_role_id}>" if staff_role_id else ""
        await channel.send(content=f"{interaction.user.mention} {mention}", embed=e, view=TicketControl(self.bot))
        await interaction.followup.send(embed=h.ok_embed("チケットを作成しました", f"{channel.mention} に移動してください。"), ephemeral=True)


class TicketControl(discord.ui.View):
    """チケット内「クローズ」ボタン（永続）。"""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🔒 クローズ", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(self.bot, interaction)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        # 手動削除されたチケットが残ると、作成上限に永久に数えられてしまう
        self.bot.store.remove_ticket(channel.id)

    # グループ全体を管理権限に縛ると、チケットを作った本人やスタッフロールの人が /チケット クローズ を使えないため、
    # コマンドごとに検査する（設置・設定=サーバー管理 / メンバー追加=チケットのスタッフ / クローズ=本人かスタッフ）
    ticket = app_commands.Group(name="チケット", description="🎫 サポートチケット（設置・設定はサーバー管理）")

    @ticket.command(name="パネル", description="チケット作成パネルを設置")
    @app_commands.rename(channel="チャンネル", title="タイトル", description="説明文")
    @app_commands.describe(channel="パネルを置くチャンネル", title="パネルのタイトル（省略時: サポート）", description="パネルの説明文（省略時: 標準の案内文）")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel,
                    title: app_commands.Range[str, 1, 200] = "サポート",
                    description: app_commands.Range[str, 1, 1000] = "下のボタンからチケットを作成してください。"):
        bot_perms = channel.permissions_for(interaction.guild.me)
        if not (bot_perms.view_channel and bot_perms.send_messages and bot_perms.embed_links):
            return await interaction.response.send_message(embed=h.error_embed("設置できません", f"Bot が {channel.mention} に埋め込みを投稿できません。チャンネルの権限を確認してください。"), ephemeral=True)
        e = h.embed(f"🎫 {title}", description, h.INFO)
        await channel.send(embed=e, view=TicketPanel(self.bot))
        done = h.ok_embed("設置しました", f"{channel.mention} にパネルを設置しました。")
        gid = interaction.guild.id
        if not self.bot.store.get_setting(gid, "ticket_staff_role"):
            done.add_field(name="⚠️ スタッフロールが未設定です",
                           value="このままだと、チケットはサーバー管理者しか見られず、スタッフも呼び出されません。`/チケット 設定` でスタッフロールを設定してください。", inline=False)
        if not self.bot.store.get_setting(gid, "ticket_log_channel"):
            done.add_field(name="ℹ️ ログチャンネルが未設定です",
                           value="未設定だと、クローズ時に会話の記録が残りません。`/チケット 設定` で設定できます。", inline=False)
        await interaction.response.send_message(embed=done, ephemeral=True)

    @ticket.command(name="設定", description="チケットの作成先カテゴリー・スタッフロール・記録の送信先を設定")
    @app_commands.rename(category="カテゴリー", staff_role="スタッフロール", log_channel="ログチャンネル", clear="解除")
    @app_commands.describe(
        category="チケットを作るカテゴリー（省略時: 変更なし）",
        staff_role="閲覧・クローズできるスタッフのロール（省略時: 変更なし）",
        log_channel="クローズ時の会話記録の送信先（省略時: 変更なし）",
        clear="設定を解除する項目（省略時: 解除しない）")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(clear=[
        app_commands.Choice(name="カテゴリー", value="ticket_category"),
        app_commands.Choice(name="スタッフロール", value="ticket_staff_role"),
        app_commands.Choice(name="ログチャンネル", value="ticket_log_channel"),
    ])
    async def config(self, interaction: discord.Interaction, category: discord.CategoryChannel = None,
                     staff_role: discord.Role = None, log_channel: discord.TextChannel = None,
                     clear: app_commands.Choice[str] = None):
        gid = interaction.guild.id
        store = self.bot.store
        if staff_role and (staff_role.is_default() or staff_role.managed):
            # @everyone を指定すると、全チケットが全員に見えてしまう
            return await interaction.response.send_message(embed=h.error_embed("設定できません", "@everyone や連携用のロールは、スタッフロールに指定できません。"), ephemeral=True)
        if category:
            store.set_setting(gid, "ticket_category", category.id)
        if staff_role:
            store.set_setting(gid, "ticket_staff_role", staff_role.id)
        if log_channel:
            store.set_setting(gid, "ticket_log_channel", log_channel.id)
        if clear:
            store.delete_setting(gid, clear.value)

        def show(key, fmt):
            v = store.get_setting(gid, key)
            return fmt % v if v else "未設定"

        e = h.ok_embed("チケット設定", "現在の設定です。")
        e.add_field(name="カテゴリー", value=show("ticket_category", "<#%s>"), inline=True)
        e.add_field(name="スタッフロール", value=show("ticket_staff_role", "<@&%s>"), inline=True)
        e.add_field(name="ログチャンネル", value=show("ticket_log_channel", "<#%s>"), inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @ticket.command(name="メンバー追加", description="このチケットにメンバーを追加")
    @app_commands.rename(user="対象")
    @app_commands.describe(user="追加するメンバー")
    async def add_user(self, interaction: discord.Interaction, user: discord.Member):
        if self.bot.store.get_ticket(interaction.channel.id) is None:
            return await interaction.response.send_message(embed=h.error_embed("使えません", "ここはチケットのチャンネルではありません。"), ephemeral=True)
        if not is_ticket_staff(self.bot.store, interaction.user):
            return await interaction.response.send_message(embed=h.error_embed("権限がありません", "チケットのスタッフ（チャンネルの管理権限、またはスタッフロール）だけが追加できます。"), ephemeral=True)
        await interaction.channel.set_permissions(user, view_channel=True, send_messages=True)
        await interaction.response.send_message(embed=h.ok_embed("追加しました", f"{user.mention} をこのチケットに追加しました。"))

    @ticket.command(name="クローズ", description="このチケットをクローズ（作成者かスタッフ・確認あり）")
    @app_commands.rename(reason="理由")
    @app_commands.describe(reason="記録に残す理由（省略可）")
    async def close_cmd(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 400] = None):
        await close_ticket(self.bot, interaction, reason)


async def setup(bot):
    cog = Tickets(bot)
    await bot.add_cog(cog)
    # 永続Viewを登録（再起動後もボタンが機能するように）
    bot.add_view(TicketPanel(bot))
    bot.add_view(TicketControl(bot))
