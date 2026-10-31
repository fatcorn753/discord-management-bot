"""Bot を使えるサーバーの管理（許可リストと、持ち主の承認）。

方式:
- config の allowed_guild_ids … あらかじめ許可しておくサーバー
- require_approval が true … 上に無いサーバーは、Bot の持ち主（owner_ids）が承認するまで使えない
- require_approval が false … 従来どおり。allowed_guild_ids が空ならどのサーバーでも使え、空でなければその中だけ
持ち主（owner_ids）は、どのサーバーでもコマンドを使える（承認の操作を、承認待ちのサーバーの中からもできるように）。
未承認のサーバーでは、コマンドだけでなく、メッセージの監視や自動対策などのイベント処理も止める（core/gated_bot.py）。
"""

import discord

from core import helpers as h

PENDING, APPROVED, DENIED = "pending", "approved", "denied"


def requires_approval(config: dict) -> bool:
    return bool(config.get("require_approval"))


def owner_ids(config: dict) -> set:
    return {int(x) for x in (config.get("owner_ids") or [])}


def preapproved(config: dict) -> set:
    return {int(x) for x in (config.get("allowed_guild_ids") or [])}


def is_allowed(store, config: dict, guild_id: int, user_id=None) -> bool:
    """このサーバーで、Bot の機能を動かしてよいか。user_id を渡すと、持ち主は常に許可（コマンド用）。"""
    if (user_id is not None and user_id in owner_ids(config)) or guild_id in preapproved(config):
        return True
    if requires_approval(config):
        row = store.get_guild_status(guild_id)
        return bool(row) and row["status"] == APPROVED
    return not preapproved(config)          # 承認制でなければ、リストが空のときだけ全サーバー可


DM_GROUP = "運営"         # Bot とのDMでだけ使える、持ち主向けのコマンドの名前


def is_owner_dm_command(interaction: discord.Interaction) -> bool:
    """Bot の持ち主が、DM で「運営」のコマンド（サーバーの許可の管理）を使うとき。サーバーの外で使える、ただ1つの場合。"""
    cmd = interaction.command
    root = (cmd.root_parent or cmd) if cmd is not None else None
    return (interaction.guild is None and getattr(root, "name", None) == DM_GROUP
            and interaction.user.id in owner_ids(interaction.client.config))


async def guard(interaction: discord.Interaction) -> bool:
    """コマンドの入口の検査。使えないサーバーには理由を返して False を返す。"""
    bot = interaction.client
    if is_allowed(bot.store, bot.config, interaction.guild_id, interaction.user.id):
        return True
    if not requires_approval(bot.config):
        await interaction.response.send_message(
            embed=h.error_embed("このサーバーでは使えません", "この Bot は、許可されたサーバーでだけ使えます。"), ephemeral=True)
        return False
    row = bot.store.get_guild_status(interaction.guild_id)
    cog = bot.get_cog("ApprovalCog")
    if cog is not None:
        await cog.ensure_requested(interaction.guild)        # 未登録なら承認を依頼する／しばらくたっていれば催促する
    if row and row["status"] == DENIED:
        msg = "このサーバーでの利用は、Bot の持ち主に許可されていません。"
    else:
        msg = "このサーバーは、まだ Bot の持ち主に承認されていません。承認を依頼しました。承認されるまで、少しお待ちください。"
    await interaction.response.send_message(embed=h.warn_embed("承認待ちです", msg), ephemeral=True)
    return False
