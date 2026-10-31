"""
軽量な永続ストレージ層。

標準ライブラリの sqlite3 のみを使用（追加依存なし）。
Bot 全体で 1 つの Store インスタンスを共有し、
- ギルドごとの設定 (kv)
- 警告 (warnings)
- リアクションロール (reaction_roles)
- チケット (tickets)
- 一時ミュート等の予約解除 (scheduled)
- チャンネル構成のバックアップ (backups)
を管理する。

sqlite3 は同期 API だが、ローカルファイルへの軽量アクセスのみで
Bot の負荷規模なら実用上ブロッキングは無視できる。安全のため
check_same_thread=False + Lock で保護する。
"""

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional


class Store:
    def __init__(self, db_path: str):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    guild_id INTEGER NOT NULL,
                    key      TEXT    NOT NULL,
                    value    TEXT    NOT NULL,
                    PRIMARY KEY (guild_id, key)
                );

                CREATE TABLE IF NOT EXISTS warnings (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id     INTEGER NOT NULL,
                    user_id      INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    reason       TEXT,
                    created_at   REAL    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reaction_roles (
                    guild_id   INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    emoji      TEXT    NOT NULL,
                    role_id    INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, message_id, emoji)
                );

                CREATE TABLE IF NOT EXISTS tickets (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id   INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    created_at REAL    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS guild_status (
                    guild_id    INTEGER PRIMARY KEY,
                    status      TEXT NOT NULL,       -- pending / approved / denied
                    name        TEXT,
                    decided_by  INTEGER,
                    updated_at  REAL NOT NULL,
                    notified_at REAL
                );

                CREATE TABLE IF NOT EXISTS backups (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id   INTEGER NOT NULL,
                    name       TEXT    NOT NULL,
                    created_at REAL    NOT NULL,
                    data       TEXT    NOT NULL,
                    UNIQUE (guild_id, name)
                );

                CREATE TABLE IF NOT EXISTS scheduled (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id   INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    action     TEXT    NOT NULL,
                    run_at     REAL    NOT NULL,
                    payload    TEXT
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # ギルド設定 (Key-Value)                                              #
    # ------------------------------------------------------------------ #
    # ---- サーバーの利用承認 ----
    def set_guild_status(self, guild_id: int, status: str, name: Optional[str] = None, by: Optional[int] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO guild_status (guild_id, status, name, decided_by, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET status=excluded.status, "
                "name=COALESCE(excluded.name, guild_status.name), decided_by=excluded.decided_by, updated_at=excluded.updated_at",
                (guild_id, status, name, by, time.time()))

    def get_guild_status(self, guild_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM guild_status WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def list_guild_status(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM guild_status ORDER BY updated_at DESC")]

    def mark_guild_notified(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE guild_status SET notified_at=? WHERE guild_id=?", (time.time(), guild_id))

    def get_setting(self, guild_id: int, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE guild_id=? AND key=?",
                (guild_id, key),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_setting(self, guild_id: int, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, json.dumps(value, ensure_ascii=False)),
            )
            self._conn.commit()

    def delete_setting(self, guild_id: int, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM kv WHERE guild_id=? AND key=?", (guild_id, key)
            )
            self._conn.commit()

    def get_kv_by_prefix(self, prefix: str) -> list:
        """全ギルド横断で key が prefix で始まる設定を返す。

        戻り値: [(guild_id, key, value), ...]（value は json デコード済み）
        Giveaway 等の再起動後の再開処理で使う。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT guild_id, key, value FROM kv WHERE substr(key, 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchall()
        out = []
        for r in rows:
            try:
                out.append((r["guild_id"], r["key"], json.loads(r["value"])))
            except (json.JSONDecodeError, TypeError):
                pass
        return out

    def all_settings(self, guild_id: int) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM kv WHERE guild_id=?", (guild_id,)
            ).fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                pass
        return out

    # ------------------------------------------------------------------ #
    # 警告                                                                #
    # ------------------------------------------------------------------ #
    def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason, time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_warnings(self, guild_id: int, user_id: int) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created_at",
                (guild_id, user_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_warning(self, warning_id: int, guild_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM warnings WHERE id=? AND guild_id=?", (warning_id, guild_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def clear_warnings(self, guild_id: int, user_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM warnings WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ #
    # リアクションロール                                                  #
    # ------------------------------------------------------------------ #
    def add_reaction_role(self, guild_id: int, message_id: int, emoji: str, role_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO reaction_roles (guild_id, message_id, emoji, role_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, message_id, emoji) DO UPDATE SET role_id=excluded.role_id",
                (guild_id, message_id, emoji, role_id),
            )
            self._conn.commit()

    def get_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
                (guild_id, message_id, emoji),
            ).fetchone()
        return row["role_id"] if row else None

    def list_reaction_roles(self, guild_id: int) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_id, emoji, role_id FROM reaction_roles WHERE guild_id=? ORDER BY message_id, emoji",
                (guild_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
                (guild_id, message_id, emoji),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # チケット                                                            #
    # ------------------------------------------------------------------ #
    def add_ticket(self, channel_id: int, guild_id: int, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tickets (channel_id, guild_id, user_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, guild_id, user_id, time.time()),
            )
            self._conn.commit()

    def get_ticket(self, channel_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tickets WHERE channel_id=?", (channel_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tickets(self, guild_id: int, user_id: int) -> list:
        """ユーザーが開いているチケットの一覧（作成順）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE guild_id=? AND user_id=? ORDER BY created_at",
                (guild_id, user_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def user_ticket_count(self, guild_id: int, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
        return row["c"]

    def remove_ticket(self, channel_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tickets WHERE channel_id=?", (channel_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 予約タスク（一時ミュート/BAN 解除など）                            #
    # ------------------------------------------------------------------ #
    def add_scheduled(self, guild_id: int, user_id: int, action: str, run_at: float, payload: dict = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO scheduled (guild_id, user_id, action, run_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, action, run_at, json.dumps(payload or {}, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def due_scheduled(self, now: float, action: Optional[str] = None) -> list:
        """実行時刻を過ぎた予約を返す。action を渡すとその種別のみ。"""
        with self._lock:
            if action is None:
                rows = self._conn.execute("SELECT * FROM scheduled WHERE run_at<=?", (now,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM scheduled WHERE run_at<=? AND action=?", (now, action)
                ).fetchall()
        return self._decode_scheduled(rows)

    def list_scheduled(self, guild_id: int, action: str) -> list:
        """ギルドの未実行の予約を実行時刻順で返す。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled WHERE guild_id=? AND action=? ORDER BY run_at, id",
                (guild_id, action),
            ).fetchall()
        return self._decode_scheduled(rows)

    @staticmethod
    def _decode_scheduled(rows) -> list:
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {}
            out.append(d)
        return out

    def remove_scheduled(self, sched_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM scheduled WHERE id=?", (sched_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # チャンネル構成バックアップ                                          #
    # ------------------------------------------------------------------ #
    def save_backup(self, guild_id: int, name: str, data: dict) -> None:
        """同名があれば上書きする。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO backups (guild_id, name, created_at, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, name) DO UPDATE SET "
                "created_at=excluded.created_at, data=excluded.data",
                (guild_id, name, time.time(), json.dumps(data, ensure_ascii=False)),
            )
            self._conn.commit()

    def get_backup(self, guild_id: int, name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM backups WHERE guild_id=? AND name=?", (guild_id, name)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            return {}  # 壊れた記録は、復元側の形式チェックで弾く

    def list_backups(self, guild_id: int) -> list:
        """[{name, created_at}, ...] を新しい順で返す（data は含めない）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, created_at FROM backups WHERE guild_id=? ORDER BY created_at DESC",
                (guild_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_backup(self, guild_id: int, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM backups WHERE guild_id=? AND name=?", (guild_id, name)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_guild_data(self, guild_id: int) -> None:
        """サーバーから Bot が退出したときに、そのサーバーのデータをすべて削除する。"""
        with self._lock:
            for table in ("kv", "warnings", "reaction_roles", "tickets", "scheduled", "backups"):
                self._conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # サーバー退出後のデータ保持（誤ってキックされても、一定期間は設定が残るように）     #
    # ------------------------------------------------------------------ #
    LEFT_PREFIX = "left_"  # guild_id=0 の kv に「退出した日時」を記録する

    def mark_guild_left(self, guild_id: int, when: float = None) -> None:
        self.set_setting(0, f"{self.LEFT_PREFIX}{guild_id}", when if when is not None else time.time())

    def clear_guild_left(self, guild_id: int) -> None:
        self.delete_setting(0, f"{self.LEFT_PREFIX}{guild_id}")

    def left_guilds(self) -> dict:
        """{サーバーID: 退出日時} を返す。"""
        return {int(key[len(self.LEFT_PREFIX):]): value
                for gid, key, value in self.get_kv_by_prefix(self.LEFT_PREFIX) if gid == 0}

    def known_guild_ids(self) -> set:
        """データが保存されているサーバーIDの一覧（退出マーカー用の 0 を除く）。"""
        ids = set()
        with self._lock:
            for table in ("kv", "warnings", "reaction_roles", "tickets", "scheduled", "backups"):
                ids.update(r[0] for r in self._conn.execute(f"SELECT DISTINCT guild_id FROM {table}").fetchall())
        ids.discard(0)
        return ids

    def close(self):
        with self._lock:
            self._conn.close()
