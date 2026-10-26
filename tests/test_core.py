"""core 層（store / helpers）と automod の回帰テスト。実行: python -m pytest tests"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.automod import SPAM_HISTORY, AutoMod  # noqa: E402
from core import helpers as h  # noqa: E402
from core.store import Store  # noqa: E402


def test_parse_duration():
    assert h.parse_duration("10m") == 600
    assert h.parse_duration("1h30m") == 5400
    assert h.parse_duration("5") == 300
    assert h.parse_duration("abc") is None
    assert h.parse_duration("10") == 600
    assert h.parse_duration("1h30") is None


def test_kv_prefix_is_literal(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.set_setting(1, "giveaway_10", {"a": 1})
    store.set_setting(1, "giveawayX10", {"a": 2})  # "_" がワイルドカード扱いなら混入する
    keys = [k for _g, k, _v in store.get_kv_by_prefix("giveaway_")]
    assert keys == ["giveaway_10"]


def test_ticket_removal(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.add_ticket(100, 1, 5)
    assert store.user_ticket_count(1, 5) == 1
    store.remove_ticket(100)
    assert store.user_ticket_count(1, 5) == 0


def test_spam_history_covers_max_count():
    # /automod spam の count 上限は 20。履歴がそれ未満だと判定が永久に成立しない
    assert SPAM_HISTORY >= 20
    dq = AutoMod(bot=None)._recent[(1, 2)]
    assert isinstance(dq, deque) and dq.maxlen >= 20


def test_parse_duration_rejects_non_ascii_and_huge_input():
    assert h.parse_duration("²") is None
    assert h.parse_duration("1²m") is None
    assert h.parse_duration("９") is None  # 全角数字
    assert h.parse_duration("9" * 5000) is None
    assert h.parse_duration("") is None


def test_store_without_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Store("bot.db").set_setting(1, "k", 1)  # dirname が空でも失敗しない


class _FakeStore:
    def __init__(self, settings):
        self.settings = dict(settings)

    def all_settings(self, _gid):
        return dict(self.settings)

    def set_setting(self, _gid, key, value):
        self.settings[key] = value


def test_migrate_channel_settings():
    from types import SimpleNamespace

    from cogs.message_tools import MessageTools

    store = _FakeStore({"log_channel": 10, "welcome_channel": 11, "ticket_category": 10, "warn_threshold": 10})
    MessageTools(SimpleNamespace(store=store))._migrate_channel_settings(1, 10, 99)
    assert store.settings == {"log_channel": 99, "welcome_channel": 11, "ticket_category": 10, "warn_threshold": 10}


def test_giveaway_marks_drawn_even_if_send_fails(tmp_path):
    import asyncio
    from types import SimpleNamespace

    import discord

    from cogs.polls import Polls

    class Channel:
        async def send(self, *a, **k):
            raise discord.HTTPException(SimpleNamespace(status=403, reason="x"), "forbidden")

        async def fetch_message(self, _mid):
            raise discord.HTTPException(SimpleNamespace(status=404, reason="x"), "not found")

    store = Store(str(tmp_path / "t.db"))
    store.set_setting(1, "giveaway_5", {"prize": "p", "winners": 1, "entrants": [7], "channel_id": 2, "end_ts": 0, "drawn": False})
    guild = SimpleNamespace(id=1, get_channel=lambda _cid: Channel())
    cog = Polls.__new__(Polls)
    cog.bot = SimpleNamespace(store=store)
    asyncio.run(cog._draw(guild, 5))
    assert store.get_setting(1, "giveaway_5")["drawn"] is True


# --------------------------- バックアップ ---------------------------- #
def test_backup_store_roundtrip(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_backup(1, "a", {"x": 1})
    store.save_backup(1, "a", {"x": 2})  # 同名は上書き
    store.save_backup(2, "a", {"x": 9})  # 他ギルドとは独立
    assert store.get_backup(1, "a") == {"x": 2}
    assert [b["name"] for b in store.list_backups(1)] == ["a"]
    assert store.delete_backup(1, "a") is True
    assert store.get_backup(1, "a") is None
    assert store.delete_backup(1, "a") is False
    assert store.get_backup(2, "a") == {"x": 9}


def _fake_guild(categories=(), loose=()):
    import discord
    from types import SimpleNamespace

    created = []

    async def make(kind, *args, **kwargs):
        created.append((kind, args[0], kwargs))
        return SimpleNamespace(name=args[0], type=discord.ChannelType.text, channels=[], category=kwargs.get("category"))

    async def create_category(name, **kwargs):
        created.append(("category", name, kwargs))
        cat = SimpleNamespace(name=name, channels=[])
        guild.categories.append(cat)
        return cat

    class Role:  # PermissionOverwrite の dict キーにするため hashable にする
        def __init__(self, name):
            self.name = name

    default_role = Role("@everyone")
    guild = SimpleNamespace(
        categories=list(categories), channels=list(loose), roles=[default_role, Role("認証済み")],
        default_role=default_role, features=[], bitrate_limit=96000,
        create_category=create_category,
        create_text_channel=lambda *a, **k: make("text", *a, **k),
        create_voice_channel=lambda *a, **k: make("voice", *a, **k),
    )
    return guild, created


def test_restore_creates_only_missing():
    import asyncio
    from types import SimpleNamespace

    import discord

    from cogs.backup import restore_snapshot

    ow = [{"role": "@everyone", "default": True, "allow": 0, "deny": 1024},
          {"role": "消えたロール", "default": False, "allow": 1024, "deny": 0}]
    data = {"version": 1, "unsupported": [], "uncategorized": [],
            "categories": [{"name": "雑談", "position": 0, "overwrites": ow, "channels": [
                {"name": "既存", "type": "text", "position": 0, "overwrites": ow},
                {"name": "新規", "type": "text", "position": 1, "overwrites": ow},
                {"name": "ボイス", "type": "voice", "position": 2, "overwrites": [], "bitrate": 384000},
            ]}]}
    existing = SimpleNamespace(name="既存", type=discord.ChannelType.text)
    cat = SimpleNamespace(name="雑談", channels=[existing])
    guild, created = _fake_guild(categories=[cat])

    result = asyncio.run(restore_snapshot(guild, data, "t"))

    assert [c[1] for c in created] == ["新規", "ボイス"]  # カテゴリーと既存チャンネルは作らない
    assert result["skipped"] == 2
    assert result["missing_roles"] == ["消えたロール"]
    assert created[1][2]["bitrate"] == 96000  # サーバー上限に丸める
    assert result["failed"] == []


def test_restore_is_idempotent_for_new_category():
    import asyncio

    from cogs.backup import restore_snapshot

    data = {"version": 1, "unsupported": [], "uncategorized": [],
            "categories": [{"name": "新カテゴリー", "position": 0, "overwrites": [], "channels": [
                {"name": "a", "type": "text", "position": 0, "overwrites": []}]}]}
    guild, created = _fake_guild()
    result = asyncio.run(restore_snapshot(guild, data, "t"))
    assert [c[1] for c in created] == ["新カテゴリー", "a"]
    assert result["created"] == ["📁 新カテゴリー", "#a"]


# ---------------------------- 予約投稿 ------------------------------- #
def _utc(*args):
    import datetime
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


def test_parse_when_formats():
    now = _utc(2026, 10, 20, 0, 0)  # JST 09:00
    assert h.parse_when("30m", now) == _utc(2026, 10, 20, 0, 30)
    assert h.parse_when("2026-10-25 18:30", now) == _utc(2026, 10, 25, 9, 30)  # JST→UTC
    assert h.parse_when("18:30", now) == _utc(2026, 10, 20, 9, 30)  # 今日
    assert h.parse_when("08:00", now) == _utc(2026, 10, 20, 23, 0)  # 過ぎていれば明日(JST 10/21 08:00)
    assert h.parse_when("10-19 12:00", now) == _utc(2027, 10, 19, 3, 0)  # 過ぎていれば来年
    assert h.parse_when("18:30", now, utc_offset_hours=0) == _utc(2026, 10, 20, 18, 30)


def test_parse_when_rejects_garbage():
    now = _utc(2026, 10, 20, 0, 0)
    for bad in ["", "abc", "2026-02-30 10:00", "25:00", "2026-13-01 10:00", "02-29 10:00x"]:
        assert h.parse_when(bad, now) is None, bad


def test_scheduled_store_filters(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    a = store.add_scheduled(1, 5, "post", 100.0, {"k": 1})
    store.add_scheduled(1, 5, "other", 100.0)
    store.add_scheduled(2, 5, "post", 100.0)
    store.add_scheduled(1, 5, "post", 999.0)
    assert [i["id"] for i in store.due_scheduled(500.0, "post")] == [a, a + 2]
    assert [i["id"] for i in store.list_scheduled(1, "post")] == [a, a + 3]
    assert store.list_scheduled(1, "post")[0]["payload"] == {"k": 1}


def _schedule_cog(tmp_path, channel, guild_present=True):
    from types import SimpleNamespace

    from cogs.schedule import Schedule

    store = Store(str(tmp_path / "t.db"))
    guild = SimpleNamespace(id=1, name='g', get_member=lambda uid: None, get_channel=lambda cid: channel if cid == 7 else None)
    bot = SimpleNamespace(store=store, config={}, get_guild=lambda gid: guild if guild_present else None)
    cog = Schedule.__new__(Schedule)
    cog.bot, cog._attempts = bot, {}
    return cog, store


def _http_error(status):
    from types import SimpleNamespace

    import discord
    return discord.HTTPException(SimpleNamespace(status=status, reason="x"), "err")


def test_process_due_posts_and_removes(tmp_path):
    import asyncio

    sent = []

    class Channel:
        async def send(self, **kwargs):
            sent.append(kwargs)

    cog, store = _schedule_cog(tmp_path, Channel())
    store.add_scheduled(1, 5, "post", 1000.0, {"channel_id": 7, "content": "こんにちは", "as_embed": False, "mention_ok": False})
    store.add_scheduled(1, 5, "post", 9999.0, {"channel_id": 7, "content": "未来", "as_embed": False})
    asyncio.run(cog.process_due(1010.0))
    assert [s["content"] for s in sent] == ["こんにちは"]
    assert sent[0]["allowed_mentions"].everyone is False
    assert [i["payload"]["content"] for i in store.list_scheduled(1, "post")] == ["未来"]


def test_process_due_skips_too_late_and_missing_channel(tmp_path):
    import asyncio

    sent = []

    class Channel:
        async def send(self, **kwargs):
            sent.append(kwargs)

    cog, store = _schedule_cog(tmp_path, Channel())
    store.add_scheduled(1, 5, "post", 1000.0, {"channel_id": 7, "content": "古い", "as_embed": False})
    store.add_scheduled(1, 5, "post", 1000.0, {"channel_id": 999, "content": "宛先なし", "as_embed": False})
    asyncio.run(cog.process_due(1000.0 + 3601))  # 1時間超の遅れ
    asyncio.run(cog.process_due(1001.0))  # 宛先なし（時間内）
    assert sent == []
    assert store.list_scheduled(1, "post") == []  # どちらも再試行されないよう削除済み


def test_process_due_retries_5xx_then_gives_up(tmp_path):
    import asyncio

    class Channel:
        async def send(self, **kwargs):
            raise _http_error(503)

    cog, store = _schedule_cog(tmp_path, Channel())
    sid = store.add_scheduled(1, 5, "post", 1000.0, {"channel_id": 7, "content": "x", "as_embed": False})
    for _ in range(2):
        asyncio.run(cog.process_due(1001.0))
        assert len(store.list_scheduled(1, "post")) == 1  # 3回目までは残る
    asyncio.run(cog.process_due(1001.0))
    assert store.list_scheduled(1, "post") == []
    assert sid not in cog._attempts


# ------------------------ コマンドの日本語化・権限 ------------------------ #
def _bot_with_cogs(tmp_path):
    import asyncio
    import glob

    import discord
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot.store = Store(str(tmp_path / "t.db"))
    bot.config = {}

    async def load():
        for f in sorted(glob.glob("cogs/*.py")):
            await bot.load_extension(f[:-3].replace("/", "."))

    asyncio.run(load())
    return bot


def _names(payload):
    yield payload["name"]
    for o in payload.get("options", []):
        yield from _names(o)


def test_command_names_follow_discord_rules(tmp_path):
    """名前は日本語中心。英字は id など必要な所だけで、Discord の規則(小文字のみ・32文字以内)を守る。"""
    import re

    bot = _bot_with_cogs(tmp_path)
    names = [n for c in bot.tree.get_commands() for n in _names(c.to_dict(bot.tree))]
    assert names
    assert [n for n in names if re.search(r"[A-Z]", n) or not 1 <= len(n) <= 32] == []
    # 日本語を含まない名前は id のような短い識別子に限る
    assert [n for n in names if not re.search(r"[ぁ-んァ-ヶ一-龥]", n)] == []


def test_permission_enforced_at_runtime(tmp_path):
    import asyncio
    from types import SimpleNamespace

    import discord
    from discord import app_commands

    bot = _bot_with_cogs(tmp_path)
    kick, ban = bot.tree.get_command("キック"), bot.tree.get_command("バン")
    warn = bot.tree.get_command("処罰").get_command("警告付与")

    def interaction(admin=False, **perms):
        # Discord は管理者に全権限のビットを付けて送ってくる
        p = discord.Permissions.all() if admin else discord.Permissions(**perms)
        return SimpleNamespace(permissions=p, client=bot, user=None)

    def allowed(cmd, it):
        try:
            return asyncio.run(cmd._check_can_run(it))
        except app_commands.MissingPermissions as exc:
            return exc.missing_permissions

    # BAN権限だけのモデレーター: バンは使えるが、キックと警告は拒否される
    ban_only = interaction(ban_members=True)
    assert allowed(ban, ban_only) is True
    assert allowed(kick, ban_only) == ["kick_members"]
    assert allowed(warn, ban_only) == ["moderate_members"]
    # キック権限だけ: キックが使える
    assert allowed(kick, interaction(kick_members=True)) is True
    # タイムアウト権限だけ: 警告は使えるがバンは拒否
    assert allowed(warn, interaction(moderate_members=True)) is True
    assert allowed(ban, interaction(moderate_members=True)) == ["ban_members"]
    # 一般メンバーは全て拒否
    for cmd in (kick, ban, warn):
        assert isinstance(allowed(cmd, interaction()), list)
    # 管理者は全部通る
    for cmd in (kick, ban, warn):
        assert allowed(cmd, interaction(admin=True)) is True


def test_default_permissions_visibility_matches_command(tmp_path):
    """一覧の表示制御(default_member_permissions)が、必要な権限と食い違わない。"""
    bot = _bot_with_cogs(tmp_path)
    perms = {c.name: c.to_dict(bot.tree).get("default_member_permissions") for c in bot.tree.get_commands()}
    perms = {k: (int(v) if v is not None else None) for k, v in perms.items()}
    assert perms["キック"] == 1 << 1  # kick_members
    assert perms["バン"] == perms["バン解除"] == 1 << 2  # ban_members
    assert perms["処罰"] == 1 << 40  # moderate_members（キック/バンの権限だけでは見えない）
    assert perms["情報"] is None and perms["ヘルプ"] is None


def test_other_groups_keep_required_permissions(tmp_path):
    import asyncio
    from types import SimpleNamespace

    import discord
    from discord import app_commands

    bot = _bot_with_cogs(tmp_path)
    cmd = bot.tree.get_command("自動対策").get_command("全体")
    it = SimpleNamespace(permissions=discord.Permissions(manage_messages=True), client=bot, user=None)
    try:
        asyncio.run(cmd._check_can_run(it))
        raise AssertionError("manage_guild 無しで通った")
    except app_commands.MissingPermissions as exc:
        assert exc.missing_permissions == ["manage_guild"]


# ----------------------- レビュー指摘の修正の回帰テスト ----------------------- #
def test_migrate_also_moves_giveaway_channel():
    from types import SimpleNamespace

    from cogs.message_tools import MessageTools

    store = _FakeStore({"giveaway_1": {"channel_id": 10, "prize": "p"}, "giveaway_2": {"channel_id": 55}, "log_channel": 10})
    MessageTools(SimpleNamespace(store=store))._migrate_channel_settings(1, 10, 99)
    assert store.settings["giveaway_1"]["channel_id"] == 99
    assert store.settings["giveaway_2"]["channel_id"] == 55
    assert store.settings["log_channel"] == 99


def test_schedule_drops_stale_rows_of_unknown_guild(tmp_path):
    import asyncio

    cog, store = _schedule_cog(tmp_path, None, guild_present=False)
    store.add_scheduled(1, 5, "post", 1000.0, {"channel_id": 7, "content": "x", "as_embed": False})
    asyncio.run(cog.process_due(1010.0))  # 猶予内: ギルドが一時的に見えないだけかもしれないので残す
    assert len(store.list_scheduled(1, "post")) == 1
    asyncio.run(cog.process_due(1000.0 + 3601))  # 1時間超: 破棄
    assert store.list_scheduled(1, "post") == []


def test_restore_passes_saved_position():
    import asyncio

    from cogs.backup import restore_snapshot

    data = {"version": 1, "unsupported": [], "uncategorized": [],
            "categories": [{"name": "新", "position": 3, "overwrites": [], "channels": [
                {"name": "a", "type": "text", "position": 2, "overwrites": []}]}]}
    guild, created = _fake_guild()
    asyncio.run(restore_snapshot(guild, data, "t"))
    assert created[0][2]["position"] == 3  # カテゴリー
    assert created[1][2]["position"] == 2  # チャンネル


class _GiveawayStore(_FakeStore):
    def __init__(self, settings):
        super().__init__(settings)
        self.deleted = []

    def get_setting(self, _gid, key, default=None):
        return self.settings.get(key, default)

    def delete_setting(self, _gid, key):
        self.deleted.append(key)


def _reroll(settings, entrants_key="giveaway_5"):
    import asyncio
    from types import SimpleNamespace

    from cogs.polls import Polls

    out = []

    async def send(**k):
        out.append(k["embed"])

    interaction = SimpleNamespace(guild=SimpleNamespace(id=1), response=SimpleNamespace(send_message=send))
    cog = Polls.__new__(Polls)
    cog.bot = SimpleNamespace(store=_GiveawayStore(settings))
    asyncio.run(Polls.reroll.callback(cog, interaction, "5"))
    return out[0], cog.bot.store


def test_reroll_requires_finished_giveaway_and_respects_winner_count():
    running = {"giveaway_5": {"prize": "p", "winners": 2, "entrants": [1, 2, 3], "drawn": False}}
    embed, store = _reroll(running)
    assert "まだ終了していません" in embed.description
    assert "winner_ids" not in store.settings["giveaway_5"]

    done = {"giveaway_5": {"prize": "p", "winners": 2, "entrants": [1, 2, 3, 4], "drawn": True, "winner_ids": [1, 2]}}
    embed, store = _reroll(done)
    winners = store.settings["giveaway_5"]["winner_ids"]
    assert sorted(winners) == [3, 4]  # 前回の当選者を除いて、当選人数ぶん選ぶ


def test_giveaway_loop_prunes_old_finished_records():
    import asyncio
    from types import SimpleNamespace

    from cogs.polls import GIVEAWAY_KEEP_SECONDS, Polls

    class Store(_GiveawayStore):
        def get_kv_by_prefix(self, prefix):
            return [(1, "giveaway_old", {"drawn": True, "end_ts": 0}),
                    (1, "giveaway_recent", {"drawn": True, "end_ts": 10 ** 10})]

    cog = Polls.__new__(Polls)
    cog.bot = SimpleNamespace(store=Store({}), get_guild=lambda gid: None)
    import time
    from unittest import mock
    with mock.patch.object(time, "time", return_value=GIVEAWAY_KEEP_SECONDS + 100):
        asyncio.run(Polls.check_giveaways.coro(cog))
    assert cog.bot.store.deleted == ["giveaway_old"]


def test_automod_recent_pruned():
    from cogs.automod import AutoMod

    cog = AutoMod(bot=None)
    cog._recent[(1, 1)].append(100.0)   # 古い
    cog._recent[(1, 2)].append(990.0)   # 新しい
    cog._prune_recent(1000.0)
    assert list(cog._recent) == [(1, 2)]


def test_confirm_view_is_shared():
    import discord

    from core.helpers import ConfirmView

    async def build():
        v = ConfirmView(1, "実行", discord.ButtonStyle.danger)
        return v.confirm.label, v.confirm.style

    import asyncio
    assert asyncio.run(build()) == ("実行", discord.ButtonStyle.danger)


# ----------------------- 使い勝手の改善の回帰テスト ----------------------- #
def test_validate_token_gives_japanese_guidance():
    for bad in ("ここにBotトークンを貼り付け", "", None, "abc"):
        try:
            h.validate_token(bad)
            raise AssertionError(f"通ってしまった: {bad!r}")
        except SystemExit as exc:
            assert "token" in str(exc) and "Developer Portal" in str(exc)
    assert h.validate_token("a" * 24 + "." + "b" * 6 + "." + "c" * 30)


def test_transcript_contains_author_content_and_attachments():
    import datetime
    from types import SimpleNamespace

    from cogs.tickets import build_transcript

    when = datetime.datetime(2026, 10, 20, 0, 0, tzinfo=datetime.timezone.utc)  # JST 09:00
    msgs = [SimpleNamespace(created_at=when, author=SimpleNamespace(__str__=lambda s: "a", id=1), content="こんにちは",
                            attachments=[SimpleNamespace(filename="x.png", url="http://u")], embeds=[])]
    msgs[0].author = type("A", (), {"__str__": lambda s: "太郎", "id": 7})()
    text = build_transcript("ticket-1", msgs)
    assert "[2026-10-20 09:00:00] 太郎 (7): こんにちは" in text and "[添付] x.png http://u" in text


def test_diagnosis_flags_missing_setup():
    from types import SimpleNamespace

    import discord

    from cogs.settings_cog import WARN, run_diagnosis

    class Role:
        mention = "@role"

        def __init__(self, pos):
            self.pos = pos

        def __ge__(self, other):
            return self.pos >= other.pos

        def __gt__(self, other):
            return self.pos > other.pos

    me_role = Role(5)
    me = SimpleNamespace(guild_permissions=discord.Permissions(send_messages=True), top_role=me_role)
    guild = SimpleNamespace(id=1, me=me, roles=[Role(1), me_role, Role(9)], get_channel=lambda cid: None,
                            get_role=lambda rid: Role(9) if rid == 77 else None)
    store = _GiveawayStore({"verify_role": 77})
    marks = run_diagnosis(guild, store)
    text = "\n".join(t for _, t in marks)
    assert "Bot の権限が足りません" in text and "ログの送信先が未設定" in text
    assert "認証ロール" in text and "Bot のロールより上" in text
    assert any(m == WARN for m, _ in marks)


def test_help_shows_only_usable_commands():
    import discord

    from cogs.help_cog import visible_sections

    def titles(p):
        return [t for t, _ in visible_sections(p)]

    member = titles(discord.Permissions())
    assert member == ["🙋 みんなが使える"]
    ban_only = visible_sections(discord.Permissions(ban_members=True))
    text = "\n".join(l for _, ls in ban_only for l in ls)
    assert "/バン" in text and "/キック" not in text and "/処罰" not in text
    admin = visible_sections(discord.Permissions.all())
    assert len(admin) == len(visible_sections(discord.Permissions(administrator=True))) == 9


def test_delete_guild_data_removes_only_that_guild(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    for gid in (1, 2):
        store.set_setting(gid, "k", 1)
        store.add_warning(gid, 5, 6, "r")
        store.add_reaction_role(gid, 10, "😀", 3)
        store.add_ticket(100 + gid, gid, 5)
        store.add_scheduled(gid, 5, "post", 1.0)
        store.save_backup(gid, "b", {})
    store.delete_guild_data(1)
    assert store.get_setting(1, "k") is None and store.get_warnings(1, 5) == []
    assert store.list_reaction_roles(1) == [] and store.list_tickets(1, 5) == []
    assert store.list_scheduled(1, "post") == [] and store.list_backups(1) == []
    assert store.get_setting(2, "k") == 1 and len(store.get_warnings(2, 5)) == 1
    assert len(store.list_reaction_roles(2)) == 1 and len(store.list_backups(2)) == 1


def test_confirm_action_outcomes(monkeypatch):
    import asyncio
    from types import SimpleNamespace


    from core import helpers

    class FakeView:
        value = None

        def __init__(self, *a, **k):
            pass

        async def wait(self):
            return None

    edits = []

    async def send_message(**k):
        return None

    async def edit(**k):
        edits.append(k["embed"].description)

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=SimpleNamespace(send_message=send_message),
                                  edit_original_response=edit)
    monkeypatch.setattr(helpers, "ConfirmView", FakeView)
    for value, word in ((True, "処理中"), (False, "キャンセル"), (None, "30秒以内")):
        FakeView.value = value
        assert asyncio.run(helpers.confirm_action(interaction, "t", "d", "go")) is value
        assert word in edits[-1]


def test_lock_and_unlock_restore_previous_state():
    import asyncio
    from types import SimpleNamespace


    from cogs.message_tools import MessageTools

    class Overwrite:
        def __init__(self, v):
            self.send_messages = v

    state = {"v": True}  # もとは @everyone に発言を明示的に許可していた
    sent = []

    class Channel:
        id = 5

        def overwrites_for(self, _role):
            return Overwrite(state["v"])

        async def set_permissions(self, _role, overwrite=None, reason=None):
            state["v"] = overwrite.send_messages

    async def send_message(**k):
        sent.append(k["embed"].title)

    everyone = object()
    guild = SimpleNamespace(id=1, default_role=everyone)
    interaction = SimpleNamespace(guild=guild, channel=Channel(), user="u", response=SimpleNamespace(send_message=send_message))
    store = _GiveawayStore({})
    store.set_setting = lambda gid, key, value: store.settings.__setitem__(key, value)
    cog = MessageTools(SimpleNamespace(store=store))

    asyncio.run(MessageTools.lock.callback(cog, interaction, None))
    assert state["v"] is False
    asyncio.run(MessageTools.unlock.callback(cog, interaction))
    assert state["v"] is True  # 「未設定」ではなく、もとの「許可」に戻る
    asyncio.run(MessageTools.unlock.callback(cog, interaction))  # ロックされていないときは何もしない
    assert state["v"] is True and "ロックされていません" in sent[-1]


# ------------------- 再レビューの指摘に対する回帰テスト ------------------- #
def test_is_id_rejects_non_ascii_and_long():
    assert h.is_id("123456789012345678") and h.is_id(" 42 ")
    assert not h.is_id("²") and not h.is_id("９") and not h.is_id("") and not h.is_id(None)
    assert not h.is_id("1" * 21) and not h.is_id("12a")


def test_parse_when_huge_relative_input_does_not_raise():
    now = _utc(2026, 10, 20, 0, 0)
    for text in ("99999999999", "9999999999d", "99999999999999999999"):
        assert h.parse_when(text, now) is None


def _role(pos, **perms):
    import discord

    class Role:
        mention = "@r"
        managed = False

        def __init__(self, pos, perms):
            self.pos = pos
            self.permissions = discord.Permissions(**perms)

        def is_default(self):
            return False

        def __ge__(self, other):
            return self.pos >= other.pos

    return Role(pos, perms)


def _interaction_for_roles(user_top, bot_top, owner=False):
    from types import SimpleNamespace

    user = SimpleNamespace(id=1, top_role=_role(user_top))
    guild = SimpleNamespace(me=SimpleNamespace(top_role=_role(bot_top)), owner_id=1 if owner else 99)
    return SimpleNamespace(user=user, guild=guild)


def test_auto_grant_role_blocks_privilege_escalation():
    it = _interaction_for_roles(user_top=5, bot_top=10)
    assert h.auto_grant_role_problem(it, _role(2, view_channel=True)) is None
    assert "管理系" in h.auto_grant_role_problem(it, _role(2, administrator=True))
    assert "管理系" in h.auto_grant_role_problem(it, _role(2, manage_roles=True))
    assert "自分と同じか上位" in h.auto_grant_role_problem(it, _role(7))   # 自分より上のロール
    assert "Bot より上位" in h.auto_grant_role_problem(it, _role(12))
    # サーバーの持ち主は自分より上のロールも指定できる（Bot より下なら）
    owner = _interaction_for_roles(user_top=5, bot_top=10, owner=True)
    assert h.auto_grant_role_problem(owner, _role(7)) is None


def test_ticket_close_keeps_channel_when_transcript_cannot_be_saved(monkeypatch):
    import asyncio
    import datetime
    from types import SimpleNamespace

    import discord

    from cogs import tickets

    deleted, followups = [], []

    class LogChannel:
        mention = "#log"

        async def send(self, **kwargs):
            raise discord.HTTPException(SimpleNamespace(status=403, reason="x"), "forbidden")

    class Channel:
        id, name, mention = 50, "チケット-0001-a", "#t"

        def history(self, **kwargs):
            async def gen():
                yield SimpleNamespace(created_at=datetime.datetime.now(datetime.timezone.utc),
                                      author=SimpleNamespace(id=1, __str__=lambda s: "a"), content="hi", attachments=[], embeds=[])
            return gen()

        async def delete(self, **kwargs):
            deleted.append(True)

    async def confirm(*a, **k):
        return True

    async def followup_send(**k):
        followups.append(k["embed"].title)

    monkeypatch.setattr(tickets.h, "confirm_action", confirm)
    store = _GiveawayStore({"ticket_log_channel": 9})
    store.get_ticket = lambda cid: {"user_id": 1}
    store.remove_ticket = lambda cid: deleted.append("removed")
    guild = SimpleNamespace(id=1, get_channel=lambda cid: LogChannel() if cid == 9 else None)
    user = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(manage_channels=False), roles=[], guild=guild, mention="@u")
    it = SimpleNamespace(channel=Channel(), guild=guild, user=user, followup=SimpleNamespace(send=followup_send),
                         response=SimpleNamespace())
    asyncio.run(tickets.close_ticket(SimpleNamespace(store=store), it))
    assert deleted == []  # チャンネルも記録も消さない
    assert followups == ["❌ クローズを中止しました"]


def test_ticket_staff_includes_staff_role_holders():
    from types import SimpleNamespace

    from cogs.tickets import is_ticket_staff

    store = _GiveawayStore({"ticket_staff_role": 7})
    guild = SimpleNamespace(id=1)
    staff = SimpleNamespace(guild=guild, guild_permissions=SimpleNamespace(manage_channels=False), roles=[SimpleNamespace(id=7)])
    other = SimpleNamespace(guild=guild, guild_permissions=SimpleNamespace(manage_channels=False), roles=[SimpleNamespace(id=8)])
    admin = SimpleNamespace(guild=guild, guild_permissions=SimpleNamespace(manage_channels=True), roles=[])
    assert is_ticket_staff(store, staff) and is_ticket_staff(store, admin) and not is_ticket_staff(store, other)


def test_departed_guild_data_kept_then_purged(tmp_path):
    from cogs.settings_cog import RETENTION_DAYS, purge_departed

    store = Store(str(tmp_path / "t.db"))
    store.set_setting(1, "k", 1)   # Bot がいる
    store.set_setting(2, "k", 2)   # 停止中に外された（マーカーなし）
    day = 86400
    t0 = 1_000_000.0
    assert purge_departed(store, {1}, t0) == []                    # 退出を検知して記録するだけ。消さない
    assert store.get_setting(2, "k") == 2 and 2 in store.left_guilds()
    assert purge_departed(store, {1}, t0 + (RETENTION_DAYS - 1) * day) == []   # 期間内は残る
    assert purge_departed(store, {1, 2}, t0 + (RETENTION_DAYS - 1) * day) == []  # 再招待 → マーカーが消える
    assert store.left_guilds() == {} and store.get_setting(2, "k") == 2
    purge_departed(store, {1}, t0 + 100 * day)                     # また外れた
    assert purge_departed(store, {1}, t0 + (100 + RETENTION_DAYS) * day) == [2]  # 期間後に削除
    assert store.get_setting(2, "k") is None and store.get_setting(1, "k") == 1


def test_automod_rechecks_edited_messages():
    import asyncio
    from types import SimpleNamespace

    import discord

    from cogs.automod import AutoMod

    punished = []
    store = _GiveawayStore({"automod_enabled": True, "automod_bad_words": ["ngword"]})
    cog = AutoMod(bot=SimpleNamespace(store=store))

    async def punish(message, reason):
        punished.append(reason)

    cog._punish_message = punish
    cog._exempt = lambda m: False  # 管理者などの免除は別に検査済み。ここでは対象メンバーとして扱う
    cog._g = lambda gid, key, default=None: store.get_setting(gid, key, default)
    author = discord.Member.__new__(discord.Member)  # isinstance(…, discord.Member) を満たす空のインスタンス
    guild = SimpleNamespace(id=1)

    def message(content):
        return SimpleNamespace(guild=guild, author=author, content=content, mentions=[], role_mentions=[])

    # 無害な文を NGワードに書き換えてもすり抜けない
    asyncio.run(cog.on_message_edit(message("hello"), message("NGWORD here")))
    assert punished == ["禁止ワードが含まれています。"]
    # 内容が変わらない編集（埋め込みの展開など）は検査しない
    punished.clear()
    asyncio.run(cog.on_message_edit(message("NGWORD"), message("NGWORD")))
    assert punished == []
