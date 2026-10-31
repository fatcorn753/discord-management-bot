"""サーバーの利用承認: 許可の判定、未承認サーバーのイベントを止めること、承認の依頼。実行: python -m pytest tests"""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from cogs.approval_cog import ApprovalCog, valid_guild_id  # noqa: E402
from core import approval  # noqa: E402
from core.gated_bot import GatedBot, guild_id_of  # noqa: E402
from core.store import Store  # noqa: E402

OWNER, FRIEND = 7, 8


def store_of(tmp_path):
    return Store(str(tmp_path / "t.db"))


def cfg(**kw):
    return {"require_approval": True, "owner_ids": [OWNER], **kw}


def test_unapproved_guild_is_refused_and_owner_is_exempt_for_commands(tmp_path):
    s = store_of(tmp_path)
    assert not approval.is_allowed(s, cfg(), 10, FRIEND)
    assert approval.is_allowed(s, cfg(), 10, OWNER)                      # 持ち主はコマンドを使える（承認の操作のため）
    assert not approval.is_allowed(s, cfg(), 10)                         # イベント（ユーザーなし）は持ち主でも通さない


def test_approved_preapproved_denied_and_default_mode(tmp_path):
    s = store_of(tmp_path)
    s.set_guild_status(10, approval.APPROVED, "g")
    assert approval.is_allowed(s, cfg(), 10)
    s.set_guild_status(10, approval.DENIED, "g")
    assert not approval.is_allowed(s, cfg(), 10)
    assert approval.is_allowed(s, cfg(allowed_guild_ids=[10]), 10)       # 設定ファイルで許可
    assert approval.is_allowed(s, {}, 10) and approval.is_allowed(s, {}, 999)           # 承認制でなければ従来どおり全サーバー可
    assert approval.is_allowed(s, {"allowed_guild_ids": [10]}, 10) and not approval.is_allowed(s, {"allowed_guild_ids": [10]}, 11)


def test_ids_in_config_may_be_strings(tmp_path):
    s = store_of(tmp_path)
    assert approval.is_allowed(s, {"require_approval": True, "owner_ids": ["7"]}, 10, OWNER)
    assert approval.is_allowed(s, {"require_approval": True, "allowed_guild_ids": ["10"]}, 10)


def test_guild_id_of_event_args():
    msg = SimpleNamespace(guild=SimpleNamespace(id=5))
    assert guild_id_of((msg,)) == 5
    assert guild_id_of((SimpleNamespace(guild_id=6),)) == 6
    assert guild_id_of(([msg],)) == 5                                    # on_bulk_message_delete のようなリスト
    assert guild_id_of((SimpleNamespace(guild=None),)) is None           # DM


def make_bot(tmp_path, config):
    bot = GatedBot(command_prefix="!", intents=discord.Intents.none(), help_command=None)
    bot.store, bot.config = store_of(tmp_path), config
    return bot


def test_events_of_unapproved_guilds_never_reach_listeners(tmp_path):
    bot = make_bot(tmp_path, cfg())
    seen = []

    class Spy(commands.Cog):
        @commands.Cog.listener()
        async def on_message(self, message):
            seen.append(("message", message.guild.id))

        @commands.Cog.listener()
        async def on_member_join(self, member):
            seen.append(("join", member.guild.id))

        @commands.Cog.listener()
        async def on_raw_reaction_add(self, payload):
            seen.append(("reaction", payload.guild_id))

        @commands.Cog.listener()
        async def on_guild_join(self, guild):
            seen.append(("guild_join", guild.id))

    async def go():
        await bot._async_setup_hook()                                      # イベントの配信に必要な初期化（テスト用）
        await bot.add_cog(Spy())
        bot.store.set_guild_status(20, approval.APPROVED, "ok")
        for gid in (10, 20):                                               # 10 は未承認、20 は承認済み
            guild = SimpleNamespace(id=gid)
            bot.dispatch("message", SimpleNamespace(guild=guild))
            bot.dispatch("member_join", SimpleNamespace(guild=guild))
            bot.dispatch("raw_reaction_add", SimpleNamespace(guild_id=gid))
        bot.dispatch("guild_join", SimpleNamespace(id=10))                 # 参加の通知は、未承認でも届く（承認の依頼のため）
        bot.dispatch("message", SimpleNamespace(guild=None))               # DM は止めない
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert not [x for x in seen if x[1] == 10 and x[0] != "guild_join"]    # 未承認(10)では、参加の通知以外が来ない
    assert ("message", 20) in seen and ("join", 20) in seen and ("reaction", 20) in seen


def test_without_approval_all_events_pass(tmp_path):
    bot = make_bot(tmp_path, {})
    seen = []

    class Spy(commands.Cog):
        @commands.Cog.listener()
        async def on_member_join(self, member):
            seen.append(member.guild.id)

    async def go():
        await bot._async_setup_hook()                                      # イベントの配信に必要な初期化（テスト用）
        await bot.add_cog(Spy())
        bot.dispatch("member_join", SimpleNamespace(guild=SimpleNamespace(id=10)))
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert seen == [10]


def test_concurrent_requests_send_one_dm_and_buttons_are_owner_only(tmp_path):
    sent = []

    class User:
        async def send(self, **kw):
            await asyncio.sleep(0.01)
            sent.append(kw)

    bot = SimpleNamespace(store=store_of(tmp_path), config=cfg(), get_user=lambda uid: User(), fetch_user=None)
    cog = ApprovalCog(bot)
    guild = SimpleNamespace(id=55, name="g", owner_id=1, member_count=3, icon=None)

    async def go():
        await asyncio.gather(*(cog.ensure_requested(guild) for _ in range(5)))
    asyncio.run(go())
    assert len(sent) == 1 and bot.store.get_guild_status(55)["status"] == approval.PENDING


def test_valid_guild_id():
    assert valid_guild_id("123") and valid_guild_id(" 123 ")
    assert not valid_guild_id("0") and not valid_guild_id("-5") and not valid_guild_id("9" * 30) and not valid_guild_id("１２３") and not valid_guild_id("abc")


def test_owner_group_is_dm_only_and_only_the_owner_may_use_it_outside_a_server():
    ctx = ApprovalCog.group.allowed_contexts
    assert ctx.dm_channel and ctx.private_channel and not ctx.guild
    assert {c.name for c in ApprovalCog.group.commands} == {"サーバー一覧", "サーバー許可", "サーバー取消"}

    def inter(root, uid, guild=None):
        return SimpleNamespace(command=SimpleNamespace(name="x", root_parent=SimpleNamespace(name=root)), guild=guild,
                               user=SimpleNamespace(id=uid), client=SimpleNamespace(config={"owner_ids": [OWNER]}))
    assert approval.is_owner_dm_command(inter("運営", OWNER))
    assert not approval.is_owner_dm_command(inter("運営", FRIEND))                          # 他の人は、DMでも使えない
    assert not approval.is_owner_dm_command(inter("処罰", OWNER))                           # ほかのコマンドは、DMで使えない
    assert not approval.is_owner_dm_command(inter("運営", OWNER, guild=SimpleNamespace(id=1)))
