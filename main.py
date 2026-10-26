"""
統合型 Discord サーバー管理 Bot — エントリポイント。

機能一覧（cogs/ 以下で実装）:
  ● モデレーション（BAN / KICK / タイムアウト / 警告 …）
  ● 自動モデレーション（スパム / 招待リンク / 大量メンション / NG ワード / 荒らし対策）
  ● メッセージ管理（一括削除 / スローモード / ロック）
  ● ログ（メッセージ・メンバー・ボイス・モデ操作）
  ● 入退室メッセージ + 自動ロール
  ● リアクションロール
  ● チケット（サポート窓口）
  ● 便利コマンド（ユーザー情報 / サーバー情報 / アバター …）
  ● 投票 / 抽選（Giveaway）

設定は config.json（トークン等）と SQLite（ギルドごとの設定）で管理する。
"""

import json
import os
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h
from core.store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COGS_DIR = os.path.join(BASE_DIR, "cogs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "data", "bot.db")


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(
            "config.json が見つかりません。config.example.json をコピーして "
            "トークンを設定してください。"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class BotTree(app_commands.CommandTree):
    """全スラッシュコマンド共通のガードとエラー応答。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=h.error_embed("利用不可", "このコマンドはサーバー内でのみ使えます。"), ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        cause = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            names = "、".join(h.PERMISSION_JP.get(p, p) for p in error.missing_permissions)
            text = f"このコマンドには次の権限が必要です: {names}"
        elif isinstance(cause, discord.Forbidden):
            text = "Bot の権限が足りないため実行できませんでした。権限とロール順位を確認してください。"
        elif isinstance(cause, discord.HTTPException):
            text = f"Discord 側のエラーで失敗しました（コード {cause.status}）。時間をおいてもう一度お試しください。続く場合は管理者へ連絡してください。"
        else:
            traceback.print_exception(type(error), error, error.__traceback__)
            text = "予期しないエラーが発生しました。続く場合は、実行したコマンドと時刻を管理者に伝えてください。"
        embed = h.error_embed("実行できませんでした", text)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass  # 3秒の応答期限切れなどで返信自体ができない場合は、ログだけ残す
        if not isinstance(error, app_commands.MissingPermissions):
            print(f"コマンドエラー: {getattr(interaction.command, 'qualified_name', '?')}: {error!r}")


config = load_config()

intents = discord.Intents.default()
intents.message_content = True   # メッセージ内容（automod / prefix コマンド用）
intents.members = True           # 入退室・メンバー情報
intents.guilds = True

bot = commands.Bot(
    command_prefix=config.get("prefix", "!"),
    intents=intents,
    help_command=None,
    tree_cls=BotTree,
    activity=discord.Game(name=config.get("status", "/ヘルプ でコマンド一覧")),
)

# 全 cog から参照する共有オブジェクトを bot にぶら下げる
bot.store = Store(DB_PATH)
bot.config = config


async def load_all_cogs():
    """cogs/ 以下（サブフォルダ含む）の *.py をすべて拡張として読み込む。失敗したモジュール名を返す。"""
    failed = []
    for root, _dirs, files in os.walk(COGS_DIR):
        for filename in files:
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            rel = os.path.relpath(os.path.join(root, filename), BASE_DIR)
            module = rel[:-3].replace(os.sep, ".")  # cogs/sub/x.py -> cogs.sub.x
            try:
                await bot.load_extension(module)
                print(f"✓ ロード: {module}")
            except Exception:
                print(f"✗ 失敗: {module}")
                traceback.print_exc()
                failed.append(module)
    return failed


@bot.event
async def on_ready():
    print("=" * 50)
    print(f"ログイン: {bot.user} (ID: {bot.user.id})")
    print(f"参加サーバー数: {len(bot.guilds)}")
    print("=" * 50)


@bot.event
async def setup_hook():
    failed = await load_all_cogs()
    if failed:
        # 同期は Discord 側の登録を丸ごと上書きするため、欠けた状態で行うとそのコマンドが消える
        print(f"⚠ 読み込みに失敗した cog があるためコマンド同期をスキップします: {', '.join(failed)}")
        return
    # スラッシュコマンドを同期。開発中は特定ギルドへ即時同期すると速い。
    test_guild = bot.config.get("test_guild_id")
    if test_guild:
        guild_obj = discord.Object(id=int(test_guild))
        # グローバル定義をギルドへコピーして即時同期。グローバル側には触らない
        # （別の場所で動いている本番の Bot が登録したコマンドを、開発用の起動で消してしまわないため）
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
        print(f"✓ テストギルド {test_guild} にコマンド同期")
        print("⚠ test_guild_id は開発用です。同じサーバーでグローバルのコマンドも登録されていると、同じコマンドが2つ表示されます。"
              "本番運用では null にしてください。")
    else:
        await bot.tree.sync()
        print("✓ グローバルにコマンド同期（反映まで最大1時間）")


def main():
    token = h.validate_token(config.get("token"))
    test_guild = config.get("test_guild_id")
    if test_guild is not None and not str(test_guild).isdigit():
        raise SystemExit("config.json の test_guild_id は数字のサーバーIDか null にしてください。")
    try:
        bot.run(token)
    except discord.LoginFailure:
        raise SystemExit("ログインに失敗しました。config.json の token が正しいか、再発行されていないか確認してください。")
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "特権インテントが許可されていません。Developer Portal → 「Bot」→「特権ゲートウェイインテント」で\n"
            "「サーバーメンバーインテント」と「メッセージ内容インテント」をオンにして保存してください。")


if __name__ == "__main__":
    main()
