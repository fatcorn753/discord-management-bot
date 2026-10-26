"""共通ヘルパー: 埋め込み生成・ログ送信・権限チェックなど。"""

import datetime
import re
from typing import Optional

import discord
from discord import app_commands

# ---- カラーパレット -------------------------------------------------- #
OK = 0x2ECC71       # 緑: 成功
INFO = 0x3498DB     # 青: 情報
WARN = 0xF1C40F     # 黄: 警告
ERROR = 0xE74C3C    # 赤: エラー
MOD = 0xE67E22      # 橙: モデレーション
LOG = 0x95A5A6      # 灰: ログ

# 内部値 → 表示名（警告しきい値到達時の処罰）
WARN_ACTION_JP = {"timeout": "タイムアウト(1時間)", "kick": "キック", "ban": "BAN"}

# 権限名 → 日本語（権限不足メッセージ用）
PERMISSION_JP = {
    "administrator": "管理者", "manage_guild": "サーバー管理", "manage_channels": "チャンネルの管理",
    "manage_messages": "メッセージの管理", "manage_roles": "ロールの管理", "kick_members": "メンバーをキック",
    "ban_members": "メンバーをBAN", "moderate_members": "メンバーをタイムアウト",
    "mention_everyone": "@everyone へのメンション", "manage_webhooks": "ウェブフックの管理",
}

MAX_DURATION_TEXT = 20  # parse_duration が受け付ける最大文字数


def embed(title: str, description: str = "", color: int = INFO) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return e


def ok_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"✅ {title}", description, OK)


def error_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"❌ {title}", description, ERROR)


def warn_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"⚠️ {title}", description, WARN)


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def send_log(bot, guild: discord.Guild, setting_key: str, log_embed: discord.Embed):
    """ギルド設定に登録されたログチャンネルへ埋め込みを送る。未設定なら無視。

    setting_key 例: "log_channel"（汎用）, "mod_log_channel"（モデ専用）
    mod 系は専用が無ければ汎用 log_channel にフォールバックする。
    """
    if guild is None:
        return
    channel_id = bot.store.get_setting(guild.id, setting_key)
    if channel_id is None and setting_key != "log_channel":
        channel_id = bot.store.get_setting(guild.id, "log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        await channel.send(embed=log_embed)
    except discord.HTTPException:
        pass


def can_target(moderator: discord.Member, target: discord.Member) -> Optional[str]:
    """モデレーターが対象に対して処罰可能か検証。不可なら理由文字列を返す。"""
    if target == moderator:
        return "自分自身を対象にすることはできません。"
    if target == target.guild.owner:
        return "サーバーオーナーを対象にすることはできません。"
    if moderator.id != moderator.guild.owner_id and target.top_role >= moderator.top_role:
        return "自分と同じかそれ以上のロールを持つメンバーは処罰できません。"
    me = target.guild.me
    if target.top_role >= me.top_role:
        return "Bot のロールが対象より低いため処罰できません。ロール順位を上げてください。"
    return None


def format_duration(seconds: int) -> str:
    """秒数を日本語の期間表記へ。"""
    periods = [("日", 86400), ("時間", 3600), ("分", 60), ("秒", 1)]
    parts = []
    for name, count in periods:
        if seconds >= count:
            value, seconds = divmod(seconds, count)
            parts.append(f"{value}{name}")
    return "".join(parts) or "0秒"


def parse_duration(text: str) -> Optional[int]:
    """"10m", "1h30m", "2d", "45s" などを秒に変換。数字のみは分として扱う。失敗時 None。"""
    text = text.strip().lower()
    # isdigit() は '²' 等も真にして int() が失敗するため ASCII の数字に限定し、桁数も制限する
    if not text or len(text) > MAX_DURATION_TEXT:
        return None
    if re.fullmatch(r"[0-9]+", text):
        return int(text) * 60
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0
    num = ""
    matched = False
    for ch in text:
        if ch in "0123456789":
            num += ch
        elif ch in units and num:
            total += int(num) * units[ch]
            num = ""
            matched = True
        else:
            return None
    if num:  # 末尾に単位なしの数字が残った
        return None
    return total if matched else None


def parse_when(text: str, now: datetime.datetime, utc_offset_hours: float = 9) -> Optional[datetime.datetime]:
    """予約日時の入力を UTC の aware datetime にする。失敗時 None。

    受け付ける形式（絶対指定は utc_offset_hours のローカル時刻として解釈）:
      "30m" "2h" "1d"        … 今からの相対
      "2026-10-25 18:30"     … 日時
      "10-25 18:30"          … 今年（過ぎていれば来年）の日時
      "18:30"                … 今日（過ぎていれば明日）の時刻
    """
    text = " ".join(text.strip().split())
    if not text:
        return None
    seconds = parse_duration(text)
    if seconds is not None:
        try:
            return now + datetime.timedelta(seconds=seconds)
        except OverflowError:  # 桁が大きすぎる入力
            return None

    tz = datetime.timezone(datetime.timedelta(hours=utc_offset_hours))
    local_now = now.astimezone(tz)
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}", text):
            local = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
        elif re.fullmatch(r"\d{1,2}-\d{1,2} \d{1,2}:\d{2}", text):
            local = datetime.datetime.strptime(f"{local_now.year}-{text}", "%Y-%m-%d %H:%M")
            if local.replace(tzinfo=tz) <= local_now:
                local = local.replace(year=local.year + 1)
        elif re.fullmatch(r"\d{1,2}:\d{2}", text):
            hm = datetime.datetime.strptime(text, "%H:%M")
            local = local_now.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0, tzinfo=None)
            if local.replace(tzinfo=tz) <= local_now:
                local += datetime.timedelta(days=1)
        else:
            return None
    except ValueError:  # 2月30日など存在しない日時
        return None
    return local.replace(tzinfo=tz).astimezone(datetime.timezone.utc)


class PermGroup(app_commands.Group):
    """実行に権限が必要なコマンドグループ。

    default_permissions はコマンド一覧の表示制御にすぎず、サーバー側で上書きされうるため、
    実行時にも権限を検査する（配下の全サブコマンドに適用される）。

    required : 全て必要な権限（一覧の表示制御にも使う）
    any_of   : どれか1つあればグループには入れる権限。サブコマンドごとに必要な権限が
               違うグループ用（細かい権限は各サブコマンドの has_permissions で検査する）。
               表示制御はビットの AND になってしまうため設定せず、一覧には全員に見える。
    """

    def __init__(self, *, required: dict = None, any_of: tuple = (), **kwargs):
        required = required or {}
        default = discord.Permissions(**required) if required else None
        super().__init__(default_permissions=default, **kwargs)
        self._required = tuple(required)
        self._any_of = tuple(any_of)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = interaction.permissions
        if perms.administrator:
            return True
        missing = [p for p in self._required if not getattr(perms, p)]
        if missing:
            raise app_commands.MissingPermissions(missing)
        if self._any_of and not any(getattr(perms, p) for p in self._any_of):
            raise app_commands.MissingPermissions(list(self._any_of))
        return True


class ConfirmView(discord.ui.View):
    """実行者のみ操作できる確認ボタン（30秒）。value: True=実行 / False=キャンセル / None=時間切れ。"""

    def __init__(self, author_id: int, label: str, style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value = None
        self.confirm.label = label
        self.confirm.style = style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("この操作を実行できるのはコマンド実行者のみです。", ephemeral=True)
            return False
        return True

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()  # 無応答だと「インタラクションに失敗」と表示される
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


async def confirm_action(interaction: discord.Interaction, title: str, description: str, label: str,
                         style: discord.ButtonStyle = discord.ButtonStyle.danger) -> Optional[bool]:
    """実行者だけに見える確認ボタンを出し、結果を True / False / None(時間切れ) で返す。

    押された後（または時間切れ後）は確認メッセージのボタンを外して結果を書き換える。
    呼び出し後はすでに応答済みなので、結果の通知は interaction.followup で行うこと。
    """
    view = ConfirmView(interaction.user.id, label, style)
    await interaction.response.send_message(embed=warn_embed(title, description), view=view, ephemeral=True)
    await view.wait()
    outcome = {True: embed("実行します", "処理中です…", INFO),
               False: embed("中止", "キャンセルしました。", INFO),
               None: embed("中止", "30秒以内に操作がなかったため、中止しました。", INFO)}[view.value]
    try:
        await interaction.edit_original_response(embed=outcome, view=None)
    except discord.HTTPException:
        pass
    return view.value


def is_id(text: str) -> bool:
    """Discord のID（ASCIIの数字だけ・20桁以内）か。str.isdigit() は '²' なども真になるため使わない。"""
    return bool(re.fullmatch(r"[0-9]{1,20}", (text or "").strip()))


# 自動で付与するロール（認証・自動ロール）に持たせてはいけない権限。
# 設定できる人（サーバー管理）が、より強い権限を全員に配れてしまうのを防ぐ
DANGEROUS_ROLE_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages",
    "kick_members", "ban_members", "moderate_members", "manage_webhooks", "mention_everyone",
)


def auto_grant_role_problem(interaction: discord.Interaction, role: discord.Role) -> Optional[str]:
    """参加者・認証者に自動で配るロールとして不適切なら、理由を返す。"""
    if role.is_default() or role.managed:
        return "@everyone や連携用のロールは指定できません。"
    if role >= interaction.guild.me.top_role:
        return "Bot より上位のロールは付与できません。Bot のロールを上へ動かしてください。"
    if role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        return "自分と同じか上位のロールは、自動付与に指定できません。"
    risky = [PERMISSION_JP.get(p, p) for p in DANGEROUS_ROLE_PERMISSIONS if getattr(role.permissions, p)]
    if risky:
        return f"管理系の権限（{'、'.join(risky[:4])}など）を持つロールは、自動付与に指定できません。閲覧権限だけのロールを使ってください。"
    return None


def validate_token(token: str) -> str:
    """トークンが未設定・雛形のままなら、原因と対処を日本語で示して終了する。"""
    token = (token or "").strip()
    # 本物のトークンは「英数字._- を . で3つに区切った」形式。雛形の日本語プレースホルダはここで弾ける
    if not re.fullmatch(r"[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}", token):
        raise SystemExit(
            "config.json の token が未設定、または Bot トークンの形式ではありません。\n"
            "  1. Discord Developer Portal → アプリ → 「Bot」→「トークンをリセット」で表示される値をコピー\n"
            "  2. config.json の \"token\" に貼り付けて保存\n"
            "  （トークンは他人に見せないでください）")
    return token
