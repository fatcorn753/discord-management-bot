"""未承認のサーバーのイベントを、各機能（cog）に渡さない Bot。

自動対策・ログ・入退室メッセージ・リアクションロールなどは、メッセージやメンバーのイベントで動く。
承認制のときは、承認されていないサーバーで、それらが動かないように、イベントの配信の入口で止める。
"""

from discord.ext import commands

from core import approval

# サーバー単位でない（または、止めてはいけない）イベント。接続の管理、サーバーへの参加・退出の通知など
PASS_EVENTS = frozenset({
    "ready", "connect", "disconnect", "resumed", "error", "shard_connect", "shard_disconnect", "shard_ready", "shard_resumed",
    "guild_join", "guild_remove", "guild_available", "guild_unavailable", "interaction", "app_command_completion",
    "command", "command_completion", "command_error", "socket_event_type", "socket_raw_receive", "socket_raw_send",
})


def guild_id_of(args) -> "int | None":
    """イベントの引数から、どのサーバーのイベントかを調べる（分からなければ None）。"""
    for a in args:
        if isinstance(a, (list, tuple)):
            if a:
                found = guild_id_of(a[:1])
                if found is not None:
                    return found
            continue
        guild = getattr(a, "guild", None)
        if guild is not None and getattr(guild, "id", None) is not None:
            return guild.id
        gid = getattr(a, "guild_id", None)
        if isinstance(gid, int):
            return gid
    return None


class GatedBot(commands.Bot):
    def guild_allowed(self, guild_id: int) -> bool:
        return approval.is_allowed(self.store, self.config, guild_id)

    def dispatch(self, event_name, /, *args, **kwargs):
        if event_name not in PASS_EVENTS and getattr(self, "store", None) is not None:
            gid = guild_id_of(args)
            if gid is not None and not self.guild_allowed(gid):
                return          # 未承認のサーバーのイベントは、どの機能にも渡さない
        super().dispatch(event_name, *args, **kwargs)
