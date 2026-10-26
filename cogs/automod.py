"""
自動モデレーション。

ギルド設定キー（/設定 automod で変更）:
  automod_enabled       : bool          全体スイッチ
  automod_anti_invite   : bool          Discord招待リンクを削除
  automod_anti_link     : bool          すべてのURLを削除
  automod_max_mentions  : int           1メッセージあたりの最大メンション数（超過で削除）
  automod_bad_words     : [str]         NGワード（含まれたら削除）
  automod_spam_count    : int           N秒内の連投しきい値
  automod_spam_window   : int           連投判定の秒数
  automod_antiraid      : bool          短時間の大量参加を検知して自動キック
  automod_antiraid_join : int           秒あたりの参加数しきい値
  automod_log_channel   : ログ送信先（未設定なら汎用 log_channel にフォールバック）
免除: 管理者・manage_messages・manage_guild 権限保持者は自動で対象外。
"""

import datetime
import re
import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h

INVITE_RE = re.compile(r"(discord\.(gg|io|me|li)/|discordapp\.com/invite/|discord\.com/invite/)", re.I)
URL_RE = re.compile(r"https?://", re.I)
SPAM_HISTORY = 20
RECENT_PRUNE_AT = 1000  # _recent がこの数を超えたら古いユーザーを掃除する


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # (guild_id, user_id) -> 直近メッセージ時刻の deque（スパム判定用）
        # maxlen は /自動対策 連投 の count 上限（20）以上でなければ判定が成立しない
        self._recent = defaultdict(lambda: deque(maxlen=SPAM_HISTORY))
        # guild_id -> 直近の参加時刻（荒らし判定用）
        self._joins = defaultdict(lambda: deque(maxlen=50))

    def _g(self, guild_id, key, default=None):
        return self.bot.store.get_setting(guild_id, key, default)

    def _exempt(self, member: discord.Member) -> bool:
        if member.bot:
            return True
        p = member.guild_permissions
        return p.administrator or p.manage_messages or p.manage_guild

    def _prune_recent(self, now: float):
        """最後の発言から1分以上たったユーザーの履歴を捨てる（メモリが増え続けないように）。"""
        stale = [k for k, dq in self._recent.items() if not dq or now - dq[-1] > 60]
        for k in stale:
            del self._recent[k]

    async def _punish_message(self, message: discord.Message, reason: str):
        try:
            await message.delete()
        except discord.HTTPException:
            return
        try:
            await message.channel.send(
                embed=h.warn_embed("メッセージを自動削除しました", f"{message.author.mention} {reason}\n誤りだと思う場合は、スタッフへ連絡してください。"),
                delete_after=12,
            )
        except discord.HTTPException:
            pass
        e = h.embed("🤖 自動対策", color=h.MOD)
        e.add_field(name="対象", value=f"{message.author} (`{message.author.id}`)", inline=False)
        e.add_field(name="理由", value=reason, inline=True)
        e.add_field(name="チャンネル", value=message.channel.mention, inline=True)
        if message.content:
            e.add_field(name="内容", value=message.content[:1000], inline=False)
        await h.send_log(self.bot, message.guild, "automod_log_channel", e)

    def _eligible(self, message: discord.Message):
        """自動対策の対象になる発言なら、サーバーIDを返す（対象外・OFF なら None）。"""
        # webhook / システムメッセージ / DM は対象外（author が Member でない場合を含む）
        if message.guild is None or not isinstance(message.author, discord.Member):
            return None
        if self._exempt(message.author):
            return None
        gid = message.guild.id
        return gid if self._g(gid, "automod_enabled", False) else None

    async def _scan(self, message: discord.Message) -> bool:
        """内容のルール（NGワード・リンク・メンション）に違反していれば削除して True を返す。"""
        gid = message.guild.id
        content = message.content or ""

        # NGワード（部分一致）
        bad_words = self._g(gid, "automod_bad_words", []) or []
        low = content.lower()
        for w in bad_words:
            if w and w.lower() in low:
                await self._punish_message(message, "禁止ワードが含まれています。")
                return True

        # 招待リンク
        if self._g(gid, "automod_anti_invite", False) and INVITE_RE.search(content):
            await self._punish_message(message, "招待リンクは禁止されています。")
            return True

        # 全リンク
        if self._g(gid, "automod_anti_link", False) and URL_RE.search(content):
            await self._punish_message(message, "リンクの投稿は許可されていません。")
            return True

        # 大量メンション
        max_mentions = self._g(gid, "automod_max_mentions", 0)
        if max_mentions and (len(message.mentions) + len(message.role_mentions)) > int(max_mentions):
            await self._punish_message(message, "メンションが多すぎます。")
            return True
        return False

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # 無害な文を投稿してから、禁止ワードやリンクに書き換えてすり抜けるのを防ぐ
        if before.content == after.content or self._eligible(after) is None:
            return
        await self._scan(after)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        gid = self._eligible(message)
        if gid is None:
            return
        if await self._scan(message):
            return

        # スパム（連投）
        count = self._g(gid, "automod_spam_count", 0)
        window = self._g(gid, "automod_spam_window", 5)
        if count:
            now = time.time()
            if len(self._recent) > RECENT_PRUNE_AT:
                self._prune_recent(now)
            dq = self._recent[(gid, message.author.id)]
            dq.append(now)
            recent = [t for t in dq if now - t <= float(window)]
            if len(recent) >= int(count):
                actions = []
                try:
                    # 他人の発言が混ざっても連投分を削り残さないよう、走査範囲は多めに取る
                    await message.channel.purge(limit=int(count) * 3, check=lambda m: m.author == message.author)
                    actions.append("連投を削除")
                except discord.HTTPException:
                    actions.append("削除に失敗（権限を確認）")
                dq.clear()
                # 短時間タイムアウト（30秒）
                try:
                    until = discord.utils.utcnow() + datetime.timedelta(seconds=30)
                    await message.author.timeout(until, reason="自動対策: スパム連投")
                    actions.append("30秒タイムアウト")
                    try:
                        await message.channel.send(
                            embed=h.warn_embed("連投を検知しました", f"{message.author.mention} 連投のため、30秒間発言できません。\n誤りだと思う場合は、スタッフへ連絡してください。"),
                            delete_after=15)
                    except discord.HTTPException:
                        pass
                except discord.HTTPException:
                    actions.append("タイムアウトに失敗（権限・ロール順位を確認）")
                e = h.embed("🤖 自動対策 — スパム検知", color=h.MOD)
                e.add_field(name="対象", value=f"{message.author} (`{message.author.id}`)", inline=False)
                e.add_field(name="対応", value=" / ".join(actions), inline=False)
                await h.send_log(self.bot, message.guild, "automod_log_channel", e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        gid = member.guild.id
        if not self._g(gid, "automod_enabled", False) or not self._g(gid, "automod_antiraid", False):
            return
        threshold = int(self._g(gid, "automod_antiraid_join", 5) or 5)
        dq = self._joins[gid]
        now = time.time()
        dq.append(now)
        recent = [t for t in dq if now - t <= 10]  # 10秒窓
        if len(recent) >= threshold:
            # 直近参加アカウントを一時ロックダウン: 新規参加を自動キック（作成7日未満のみ）
            age_days = (discord.utils.utcnow() - member.created_at).days
            limit_days = int(self._g(gid, "automod_antiraid_age", 7) or 7)
            if age_days < limit_days:
                try:
                    await member.send(embed=h.warn_embed(
                        "参加が取り消されました",
                        f"{member.guild.name} では短時間に大量の参加があり、荒らし対策として新しいアカウントの参加を自動的に取り消しました。\n"
                        "誤りの場合は、少し時間をおいてから、もう一度参加してください。"))
                except discord.HTTPException:
                    pass
                try:
                    await member.kick(reason="荒らし対策: 短時間の大量参加を検知")
                    result = "を自動キック"
                except discord.HTTPException:
                    result = "のキックに失敗（Bot の権限・ロール順位を確認）"
                e = h.embed("🚨 大量参加の検知", f"{member} {result}（アカウント作成 {age_days} 日）", h.ERROR)
                await h.send_log(self.bot, member.guild, "automod_log_channel", e)

    # ------------------------- 設定サブコマンド ------------------------ #
    automod = h.PermGroup(name="自動対策", description="🤖 スパム・リンク・NGワード・大量参加の自動対策（権限: サーバー管理）", required={"manage_guild": True})

    def _reply(self, guild_id: int, title: str, text: str) -> discord.Embed:
        """設定変更の返信。全体スイッチが OFF のままなら、まだ動かないことを添える。"""
        e = h.ok_embed(title, text)
        if not self._g(guild_id, "automod_enabled", False):
            e.add_field(name="⚠️ まだ動作しません",
                        value="自動対策の全体スイッチが OFF です。`/自動対策 全体 有効:True` で有効にしてください。", inline=False)
        return e

    @automod.command(name="全体", description="自動対策の全体スイッチ（ON/OFF）")
    @app_commands.rename(enabled="有効")
    @app_commands.describe(enabled="ON にすると、下の個別設定が動き始めます")
    async def toggle(self, interaction: discord.Interaction, enabled: bool):
        self.bot.store.set_setting(interaction.guild.id, "automod_enabled", enabled)
        await interaction.response.send_message(embed=h.ok_embed("自動対策", f"全体スイッチを **{'ON' if enabled else 'OFF'}** にしました。"), ephemeral=True)

    @automod.command(name="招待リンク", description="Discord招待リンクを自動削除（ON/OFF）")
    @app_commands.rename(enabled="有効")
    @app_commands.describe(enabled="ON にすると、招待リンクを含む投稿を削除します")
    async def anti_invite(self, interaction: discord.Interaction, enabled: bool):
        self.bot.store.set_setting(interaction.guild.id, "automod_anti_invite", enabled)
        await interaction.response.send_message(embed=self._reply(interaction.guild.id, "招待リンク対策", f"{'ON' if enabled else 'OFF'} にしました。"), ephemeral=True)

    @automod.command(name="全リンク", description="すべてのURLを自動削除（ON/OFF）")
    @app_commands.rename(enabled="有効")
    @app_commands.describe(enabled="ON にすると、http(s):// を含む投稿をすべて削除します")
    async def anti_link(self, interaction: discord.Interaction, enabled: bool):
        self.bot.store.set_setting(interaction.guild.id, "automod_anti_link", enabled)
        await interaction.response.send_message(embed=self._reply(interaction.guild.id, "全リンク対策", f"{'ON' if enabled else 'OFF'} にしました。"), ephemeral=True)

    @automod.command(name="メンション", description="1メッセージのメンション数に上限を設定")
    @app_commands.rename(max_count="上限")
    @app_commands.describe(max_count="許可する最大メンション数 0〜50（0で無効）")
    async def max_mentions(self, interaction: discord.Interaction, max_count: app_commands.Range[int, 0, 50]):
        self.bot.store.set_setting(interaction.guild.id, "automod_max_mentions", max_count)
        await interaction.response.send_message(embed=self._reply(interaction.guild.id, "メンション制限", f"上限を {max_count} にしました（0 は無効）。"), ephemeral=True)

    @automod.command(name="連投", description="連投スパムの検知条件を設定")
    @app_commands.rename(count="回数", window_seconds="秒数")
    @app_commands.describe(count="この回数に達したら検知 2〜20（0で無効）", window_seconds="回数を数える秒数 1〜60（省略時: 5）")
    async def spam(self, interaction: discord.Interaction, count: app_commands.Range[int, 0, 20], window_seconds: app_commands.Range[int, 1, 60] = 5):
        if count == 1:
            # 1回で検知にすると、すべての発言が削除され、全員が30秒タイムアウトされてしまう
            return await interaction.response.send_message(embed=h.error_embed("回数が少なすぎます", "2以上を指定してください（無効にするなら 0）。"), ephemeral=True)
        self.bot.store.set_setting(interaction.guild.id, "automod_spam_count", count)
        self.bot.store.set_setting(interaction.guild.id, "automod_spam_window", window_seconds)
        await interaction.response.send_message(embed=self._reply(interaction.guild.id, "連投対策", f"{window_seconds}秒以内に{count}回でスパムと判定します（0 は無効）。"), ephemeral=True)

    @automod.command(name="大量参加", description="短時間の大量参加を自動キック（荒らし対策）")
    @app_commands.rename(enabled="有効", joins_per_10s="参加数", min_age_days="経過日数")
    @app_commands.describe(
        enabled="ON にすると、大量参加を検知したとき新しいアカウントを自動キックします",
        joins_per_10s="10秒間にこの人数が参加したら発動 2〜30（省略時: 5）",
        min_age_days="アカウント作成からこの日数未満の人だけキック 1〜365（省略時: 7）")
    async def antiraid(self, interaction: discord.Interaction, enabled: bool,
                       joins_per_10s: app_commands.Range[int, 2, 30] = 5, min_age_days: app_commands.Range[int, 1, 365] = 7):
        store = self.bot.store
        store.set_setting(interaction.guild.id, "automod_antiraid", enabled)
        if enabled:  # OFF にするだけのとき、以前の設定値を既定値で上書きしない
            store.set_setting(interaction.guild.id, "automod_antiraid_join", joins_per_10s)
            store.set_setting(interaction.guild.id, "automod_antiraid_age", min_age_days)
        joins_per_10s = store.get_setting(interaction.guild.id, "automod_antiraid_join", joins_per_10s)
        min_age_days = store.get_setting(interaction.guild.id, "automod_antiraid_age", min_age_days)
        text = f"{'ON' if enabled else 'OFF'}（10秒に{joins_per_10s}人でトリガー、作成{min_age_days}日未満のアカウントが対象）。\nキックされた人にはDMで事情を伝えます。"
        await interaction.response.send_message(embed=self._reply(interaction.guild.id, "大量参加対策", text), ephemeral=True)

    @automod.command(name="ワード追加", description="NGワードを追加")
    @app_commands.rename(word="ワード")
    @app_commands.describe(word="NGワードにする語句（大文字小文字は区別しません）")
    async def add_word(self, interaction: discord.Interaction, word: app_commands.Range[str, 1, 100]):
        word = word.strip()
        if not word:
            # 空文字は全メッセージに一致し、全投稿が削除されてしまうため拒否する
            return await interaction.response.send_message(embed=h.error_embed("入力エラー", "空のワードは登録できません。"), ephemeral=True)
        words = self.bot.store.get_setting(interaction.guild.id, "automod_bad_words", []) or []
        if word.lower() in [w.lower() for w in words]:
            return await interaction.response.send_message(embed=h.warn_embed("登録済みです", "そのワードは既に登録されています。"), ephemeral=True)
        words.append(word)
        self.bot.store.set_setting(interaction.guild.id, "automod_bad_words", words)
        await interaction.response.send_message(embed=self._reply(interaction.guild.id, "NGワードを追加しました", f"登録数: {len(words)}"), ephemeral=True)

    async def _word_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.permissions.manage_guild:
            return []  # コマンドの表示設定が上書きされていても、NGワードを見せない
        words = self.bot.store.get_setting(interaction.guild.id, "automod_bad_words", []) or []
        return [app_commands.Choice(name=w[:100], value=w[:100]) for w in words if current.lower() in w.lower()][:25]

    @automod.command(name="ワード削除", description="NGワードを削除")
    @app_commands.rename(word="ワード")
    @app_commands.describe(word="NGワードから外す語句（候補から選べます）")
    @app_commands.autocomplete(word=_word_autocomplete)
    async def remove_word(self, interaction: discord.Interaction, word: str):
        words = self.bot.store.get_setting(interaction.guild.id, "automod_bad_words", []) or []
        left = [w for w in words if w.lower() != word.lower()]
        if len(left) == len(words):
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", "そのワードは登録されていません。`/自動対策 ワード一覧` で確認できます。"), ephemeral=True)
        self.bot.store.set_setting(interaction.guild.id, "automod_bad_words", left)
        await interaction.response.send_message(embed=h.ok_embed("NGワードを削除しました", f"登録数: {len(left)}"), ephemeral=True)

    @automod.command(name="ワード一覧", description="登録済みのNGワードを一覧表示")
    async def list_words(self, interaction: discord.Interaction):
        words = self.bot.store.get_setting(interaction.guild.id, "automod_bad_words", []) or []
        if not words:
            return await interaction.response.send_message(embed=h.embed("NGワード", "登録されているNGワードはありません。", h.INFO), ephemeral=True)
        # 本人だけに見える返信（ephemeral）。NGワード自体が不適切な語のこともあるため
        body = "\n".join(f"・{w}" for w in words)[:3900]
        await interaction.response.send_message(embed=h.embed(f"NGワード（{len(words)}件）", body, h.INFO), ephemeral=True)

    @automod.command(name="状況", description="現在の自動対策の設定を表示")
    async def status(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        words = self.bot.store.get_setting(gid, "automod_bad_words", []) or []

        def onoff(key):
            return "ON" if self._g(gid, key) else "OFF"

        e = h.embed("🤖 自動対策の設定", color=h.INFO)
        e.add_field(name="全体スイッチ", value=onoff("automod_enabled"), inline=True)
        e.add_field(name="招待リンク削除", value=onoff("automod_anti_invite"), inline=True)
        e.add_field(name="全リンク削除", value=onoff("automod_anti_link"), inline=True)
        mentions = self._g(gid, "automod_max_mentions", 0)
        e.add_field(name="最大メンション数", value=str(mentions) if mentions else "無効", inline=True)
        spam = self._g(gid, "automod_spam_count", 0)
        e.add_field(name="連投", value=f"{self._g(gid, 'automod_spam_window', 5)}秒に{spam}回" if spam else "無効", inline=True)
        raid = self._g(gid, "automod_antiraid")
        raid_text = (f"ON（10秒に{self._g(gid, 'automod_antiraid_join', 5)}人・作成{self._g(gid, 'automod_antiraid_age', 7)}日未満）"
                     if raid else "OFF")
        e.add_field(name="大量参加", value=raid_text, inline=True)
        e.add_field(name="NGワード", value=f"{len(words)}件（`/自動対策 ワード一覧`）", inline=True)
        e.set_footer(text="対象外: 管理者・メッセージ管理・サーバー管理の権限を持つ人と Bot。NGワードは部分一致、編集後も検査します。")
        if not self._g(gid, "automod_enabled"):
            e.add_field(name="⚠️ 全体スイッチが OFF です", value="個別設定は、全体スイッチを ON にするまで動作しません。", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
