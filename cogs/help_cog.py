"""ヘルプ: 自分が使えるコマンドを、機能ごとに一覧する。"""

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h

# (見出し, [(必要な権限 or None, 説明), ...])。権限が None の行は誰にでも表示する
SECTIONS = [
    ("🙋 みんなが使える", [
        (None, "ボタン — 認証パネルの「✅ 認証する」、チケットパネルの「🎫 チケット作成」"),
        (None, "`/情報 ユーザー` `サーバー` `ロール` `アバター` `応答速度` — 各種情報の表示"),
        (None, "`/イベント 投票` — 選択肢つきの投票を作る"),
    ]),
    ("🔨 処罰", [
        ("kick_members", "`/キック` — メンバーをキック"),
        ("ban_members", "`/バン` `/バン解除` — BAN と解除（退出済みの人はID指定）"),
        ("moderate_members", "`/処罰 タイムアウト` `タイムアウト解除` — 一時的な発言禁止"),
        ("moderate_members", "`/処罰 警告付与` `警告一覧` `警告削除` `警告全消去` — 警告の管理"),
    ]),
    ("🤖 自動対策", [
        ("manage_guild", "`/自動対策 全体` — 全体のON/OFF（最初にこれをONにします）"),
        ("manage_guild", "`/自動対策 招待リンク` `全リンク` `メンション` `連投` `大量参加` — 各種対策"),
        ("manage_guild", "`/自動対策 ワード追加` `ワード削除` `ワード一覧` `状況` — NGワードと確認"),
    ]),
    ("🧹 管理", [
        ("manage_messages", "`/管理 一括削除` — メッセージをまとめて削除（13日以内）"),
        ("manage_channels", "`/管理 全消去` — チャンネルの全投稿を消去（作り直し）"),
        ("manage_channels", "`/管理 スローモード` `ロック` `ロック解除`"),
        ("manage_messages", "`/管理 お知らせ` `代理発言` — Bot から投稿"),
    ]),
    ("🔐 認証・入退室", [
        ("manage_guild", "`/認証 設置` `状況` `解除` — 荒らし対策の認証ゲート"),
        ("manage_guild", "`/入退室 参加設定` `退出設定` `参加解除` `退出解除` `自動ロール` `プレビュー`"),
    ]),
    ("🎭 ロール・チケット", [
        ("manage_roles", "`/ロール 付与` `剥奪` — ロールの付与と剥奪"),
        ("manage_roles", "`/ロール リアクション登録` `リアクション削除` `リアクション一覧` — リアクションロール"),
        ("manage_guild", "`/チケット パネル` `設定` `メンバー追加` `クローズ` — サポート窓口"),
    ]),
    ("🕒 予約・バックアップ", [
        ("manage_messages", "`/予約投稿 追加` `一覧` `取消` — 日時指定の自動投稿"),
        ("administrator", "`/バックアップ 保存` `一覧` `復元` `削除` — チャンネル構成の記録と復元"),
    ]),
    ("🎉 イベント", [
        ("manage_guild", "`/イベント 抽選` `抽選終了` `再抽選` — 抽選の開催と管理"),
    ]),
    ("🔧 設定", [
        ("manage_guild", "`/設定 診断` — 導入直後のチェック（最初に実行しましょう）"),
        ("manage_guild", "`/設定 ログ` `モデログ` `自動対策ログ` — ログの送信先"),
        ("manage_guild", "`/設定 警告しきい値` `状況` — 警告の自動処罰と設定の確認"),
    ]),
]


def visible_sections(permissions: discord.Permissions) -> list:
    """権限に応じて、使えるコマンドだけを残した [(見出し, [説明, ...]), ...] を返す。"""
    out = []
    for title, rows in SECTIONS:
        lines = [text for perm, text in rows
                 if perm is None or permissions.administrator or getattr(permissions, perm)]
        if lines:
            out.append((title, lines))
    return out


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ヘルプ", description="📖 使えるコマンドの一覧を表示")
    async def help(self, interaction: discord.Interaction):
        sections = visible_sections(interaction.permissions)
        e = h.embed("📖 コマンド一覧", "あなたが使えるコマンドを表示しています。`/` を押して名前を打つと、説明つきで選べます。", h.INFO)
        for title, lines in sections:
            e.add_field(name=title, value="\n".join(lines), inline=False)
        if len(sections) == 1:
            e.add_field(name="運営の方へ", value="管理用のコマンドは、権限を持つ人にだけ表示されます。", inline=False)
        links = []
        cfg = self.bot.config
        for key, label in (("support_url", "サポート"), ("privacy_url", "プライバシー"), ("terms_url", "利用規約")):
            if cfg.get(key):
                links.append(f"[{label}]({cfg[key]})")
        if links:
            e.add_field(name="リンク", value=" ・ ".join(links), inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))
