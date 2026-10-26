"""
チャンネル構成のバックアップ / 復元。

/バックアップ 保存 : 現在のカテゴリー・チャンネルの構成を名前を付けて DB に記録する
/バックアップ 復元 : 記録した構成のうち、サーバーに「無いものだけ」を作り直す
                     （既存のカテゴリー・チャンネルは変更・削除しない。並び順は保存時の位置を
                     指定して作るベストエフォートで、既存との兼ね合いでずれることがある）
/バックアップ 一覧 / 削除

記録するもの : カテゴリー・チャンネルの名前・種類・順序・トピック・スローモード等と、
               ロール単位の権限上書き（ロールは名前で対応づける）
記録しないもの: メッセージ、ロール自体、メンバー個人への権限上書き（個人情報を残さないため）
"""

import datetime

import discord
from discord import app_commands
from discord.ext import commands

from core import helpers as h

SCHEMA_VERSION = 1
MAX_BACKUPS_PER_GUILD = 10

# 記録・復元の対象にするチャンネル種別（それ以外は報告のうえスキップ）
SUPPORTED_TYPES = {
    discord.ChannelType.text: "text",
    discord.ChannelType.news: "news",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
}


# --------------------------------------------------------------------- #
# スナップショット（discord オブジェクト → dict）                        #
# --------------------------------------------------------------------- #
def _serialize_overwrites(channel) -> list:
    out = []
    for target, ow in channel.overwrites.items():
        if not isinstance(target, discord.Role):
            continue  # メンバー個人の上書きは記録しない
        allow, deny = ow.pair()
        if allow.value == 0 and deny.value == 0:
            continue
        out.append({"role": target.name, "default": target.is_default(),
                    "allow": allow.value, "deny": deny.value})
    return out


def _serialize_channel(channel) -> dict:
    data = {
        "name": channel.name,
        "type": SUPPORTED_TYPES[channel.type],
        "position": channel.position,
        "overwrites": _serialize_overwrites(channel),
    }
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        data["topic"] = channel.topic
        data["nsfw"] = channel.nsfw
        data["slowmode"] = channel.slowmode_delay
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        data["bitrate"] = channel.bitrate
        data["user_limit"] = getattr(channel, "user_limit", 0)
    return data


def snapshot_guild(guild: discord.Guild) -> dict:
    """現在のチャンネル構成を JSON 化できる dict にする。"""
    categories = []
    for cat in guild.categories:
        categories.append({
            "name": cat.name,
            "position": cat.position,
            "overwrites": _serialize_overwrites(cat),
            "channels": [_serialize_channel(c) for c in cat.channels if c.type in SUPPORTED_TYPES],
        })
    loose = [_serialize_channel(c) for c in guild.channels
             if c.category is None and c.type in SUPPORTED_TYPES]
    skipped = [c.name for c in guild.channels
               if not isinstance(c, discord.CategoryChannel) and c.type not in SUPPORTED_TYPES]
    return {"version": SCHEMA_VERSION, "categories": categories,
            "uncategorized": loose, "unsupported": skipped}


def count_snapshot(data: dict) -> tuple:
    """(カテゴリー数, チャンネル数)"""
    chans = sum(len(c["channels"]) for c in data["categories"]) + len(data["uncategorized"])
    return len(data["categories"]), chans


# --------------------------------------------------------------------- #
# 復元（dict → サーバー）                                                #
# --------------------------------------------------------------------- #
def _build_overwrites(guild: discord.Guild, items: list, missing_roles: set) -> dict:
    result = {}
    for item in items:
        role = guild.default_role if item["default"] else discord.utils.get(guild.roles, name=item["role"])
        if role is None:
            missing_roles.add(item["role"])
            continue
        result[role] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(item["allow"]), discord.Permissions(item["deny"]))
    return result


def _find_channel(guild_channels, name: str, kind: str):
    """同名・同種別のチャンネルを探す（Discord は名前を小文字化するため比較も小文字で行う）。"""
    for ch in guild_channels:
        if ch.name.lower() == name.lower() and SUPPORTED_TYPES.get(ch.type) == kind:
            return ch
    return None


async def _create_channel(guild, category, ch: dict, overwrites: dict, reason: str):
    kind = ch["type"]
    position = ch.get("position", 0)  # 保存時の並び順（範囲外は Discord 側で丸められる＝ベストエフォート）
    if kind in ("text", "news"):
        # ニュース(アナウンス)チャンネルはコミュニティ有効時のみ作れるため、無ければ通常テキストにする
        kwargs = dict(category=category, topic=ch.get("topic"), nsfw=ch.get("nsfw", False),
                      slowmode_delay=ch.get("slowmode", 0), overwrites=overwrites, reason=reason, position=position)
        if kind == "news" and "COMMUNITY" in guild.features:
            kwargs["news"] = True
        return await guild.create_text_channel(ch["name"], **kwargs)
    if kind == "forum":
        return await guild.create_forum(ch["name"], category=category, topic=ch.get("topic") or "",
                                        nsfw=ch.get("nsfw", False), slowmode_delay=ch.get("slowmode", 0),
                                        overwrites=overwrites, reason=reason, position=position)
    bitrate = min(ch.get("bitrate", 64000), guild.bitrate_limit)  # ブーストが減っていても作れるように
    if kind == "voice":
        return await guild.create_voice_channel(ch["name"], category=category, bitrate=bitrate,
                                                user_limit=ch.get("user_limit", 0),
                                                overwrites=overwrites, reason=reason, position=position)
    return await guild.create_stage_channel(ch["name"], category=category, bitrate=bitrate,
                                            overwrites=overwrites, reason=reason, position=position)


async def restore_snapshot(guild: discord.Guild, data: dict, reason: str) -> dict:
    """snapshot に無くサーバーに無いものだけを作る。既存は一切変更しない。

    戻り値: {"created": [...], "skipped": int, "failed": [...], "missing_roles": [...]}
    """
    created, failed, missing_roles = [], [], set()
    skipped = 0

    async def ensure_channel(ch: dict, category):
        nonlocal skipped
        pool = category.channels if category else [c for c in guild.channels if c.category is None]
        if _find_channel(pool, ch["name"], ch["type"]):
            skipped += 1
            return
        try:
            await _create_channel(guild, category, ch, _build_overwrites(guild, ch["overwrites"], missing_roles), reason)
            created.append(f"#{ch['name']}")
        except discord.HTTPException as exc:
            failed.append(f"#{ch['name']}（{exc.text or exc.status}）")

    for cat_def in data["categories"]:
        category = discord.utils.get(guild.categories, name=cat_def["name"])
        if category is None:
            try:
                category = await guild.create_category(
                    cat_def["name"], overwrites=_build_overwrites(guild, cat_def["overwrites"], missing_roles),
                    position=cat_def.get("position", 0), reason=reason)
                created.append(f"📁 {cat_def['name']}")
            except discord.HTTPException as exc:
                failed.append(f"📁 {cat_def['name']}（{exc.text or exc.status}）")
                continue  # カテゴリーが無いと配下を正しい場所に作れない
        else:
            skipped += 1
        for ch in sorted(cat_def["channels"], key=lambda c: c["position"]):
            await ensure_channel(ch, category)
    for ch in sorted(data["uncategorized"], key=lambda c: c["position"]):
        await ensure_channel(ch, None)
    return {"created": created, "skipped": skipped, "failed": failed, "missing_roles": sorted(missing_roles)}


# --------------------------------------------------------------------- #
class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = h.PermGroup(name="バックアップ", description="📦 チャンネル・カテゴリー構成の保存と復元（権限: 管理者）", required={"administrator": True})

    async def _name_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.permissions.administrator:
            return []  # コマンドの表示設定が上書きされていても、名前を見せない
        names = [b["name"] for b in self.bot.store.list_backups(interaction.guild.id)]
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]

    @group.command(name="保存", description="現在のカテゴリー・チャンネル構成を保存（同名は上書き）")
    @app_commands.rename(name="名前")
    @app_commands.describe(name="保存するバックアップ名（同じ名前は上書き）")
    async def save(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]):
        store = self.bot.store
        exists = any(b["name"] == name for b in store.list_backups(interaction.guild.id))
        if not exists and len(store.list_backups(interaction.guild.id)) >= MAX_BACKUPS_PER_GUILD:
            return await interaction.response.send_message(
                embed=h.error_embed("上限", f"バックアップは {MAX_BACKUPS_PER_GUILD} 件までです。不要なものを `/バックアップ 削除` してください。"),
                ephemeral=True)
        if exists:
            # 壊れた状態で同名を保存して、良いバックアップを失う事故を防ぐ
            ok = await h.confirm_action(interaction, "上書きしますか？",
                                        f"**{name}** はすでにあります。上書きすると、以前の内容には戻せません。", "上書きする")
            if ok is not True:
                return
            reply = interaction.followup.send
        else:
            reply = interaction.response.send_message
        data = snapshot_guild(interaction.guild)
        store.save_backup(interaction.guild.id, name, data)
        cats, chans = count_snapshot(data)
        desc = f"**{name}** を{'上書き' if exists else '保存'}しました。\nカテゴリー {cats} 件 / チャンネル {chans} 件"
        if data["unsupported"]:
            desc += f"\n⚠️ 非対応の種類のため記録していません: {', '.join(data['unsupported'])[:500]}"
        await reply(embed=h.ok_embed("保存しました", desc), ephemeral=True)

    @group.command(name="一覧", description="保存済みのバックアップを一覧表示")
    async def list_(self, interaction: discord.Interaction):
        backups = self.bot.store.list_backups(interaction.guild.id)
        if not backups:
            return await interaction.response.send_message(embed=h.embed("バックアップ", "保存されたバックアップはありません。", h.INFO), ephemeral=True)
        lines = [f"・**{b['name']}** — {discord.utils.format_dt(datetime.datetime.fromtimestamp(b['created_at'], datetime.timezone.utc), 'f')}"
                 for b in backups]
        await interaction.response.send_message(embed=h.embed("📦 バックアップ一覧", "\n".join(lines), h.INFO), ephemeral=True)

    @group.command(name="復元", description="保存した構成のうち、サーバーに無いものだけを作成")
    @app_commands.rename(name="名前")
    @app_commands.describe(name="復元するバックアップ名（候補から選べます）")
    @app_commands.autocomplete(name=_name_autocomplete)
    async def restore(self, interaction: discord.Interaction, name: str):
        guild = interaction.guild
        data = self.bot.store.get_backup(guild.id, name)
        if data is None:
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", f"バックアップ **{name}** は存在しません。"), ephemeral=True)
        if not guild.me.guild_permissions.manage_channels:
            return await interaction.response.send_message(embed=h.error_embed("権限不足", "Bot に「チャンネルの管理」権限が必要です。"), ephemeral=True)

        if data.get("version") != SCHEMA_VERSION or "categories" not in data:
            return await interaction.response.send_message(embed=h.error_embed("復元できません", f"バックアップ **{name}** の形式が、この Bot のバージョンと合いません。"), ephemeral=True)
        cats, chans = count_snapshot(data)
        ok = await h.confirm_action(
            interaction, "復元しますか？",
            f"**{name}**（カテゴリー {cats} / チャンネル {chans}）のうち、サーバーに無いものだけを作成します。\n"
            "既存のカテゴリー・チャンネルは変更・削除しません。", "復元する", discord.ButtonStyle.primary)
        if ok is not True:
            return

        result = await restore_snapshot(guild, data, reason=f"バックアップ復元: {name} ({interaction.user})")
        report = h.embed("📦 復元結果", color=h.OK if not result["failed"] else h.WARN)
        report.add_field(name=f"作成 ({len(result['created'])})", value="\n".join(result["created"])[:1024] or "なし", inline=False)
        report.add_field(name="既存のためスキップ", value=str(result["skipped"]), inline=True)
        if result["failed"]:
            report.add_field(name=f"失敗 ({len(result['failed'])})", value="\n".join(result["failed"])[:1024], inline=False)
        if result["missing_roles"]:
            report.add_field(name="見つからないロール（その権限上書きは省略）",
                             value=", ".join(result["missing_roles"])[:1024], inline=False)
        await interaction.followup.send(embed=report, ephemeral=True)
        await h.send_log(self.bot, guild, "mod_log_channel",
                         h.embed("📦 バックアップ復元", f"{interaction.user.mention} が **{name}** を復元（作成 {len(result['created'])} 件）", h.MOD))

    @group.command(name="削除", description="バックアップを削除")
    @app_commands.rename(name="名前")
    @app_commands.describe(name="削除するバックアップ名（候補から選べます）")
    @app_commands.autocomplete(name=_name_autocomplete)
    async def delete(self, interaction: discord.Interaction, name: str):
        if self.bot.store.get_backup(interaction.guild.id, name) is None:
            return await interaction.response.send_message(embed=h.error_embed("見つかりません", f"**{name}** は存在しません。"), ephemeral=True)
        ok = await h.confirm_action(interaction, "バックアップを削除しますか？", f"**{name}** を削除します（元に戻せません）。", "削除する")
        if ok is not True:
            return
        self.bot.store.delete_backup(interaction.guild.id, name)
        await interaction.followup.send(embed=h.ok_embed("削除しました", f"**{name}** を削除しました。"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Backup(bot))
