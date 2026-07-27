"""SQLite storage for mailer accounts, groups, messages, settings, logs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite


class MailerDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._migrate()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "DB not connected"
        return self._db

    async def _migrate(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT '',
                session_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                sent_in_cycle INTEGER NOT NULL DEFAULT 0,
                cycle_started_at REAL,
                next_cycle_at REAL,
                last_sent_at REAL,
                last_error TEXT,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                last_sent_at REAL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'default',
                text TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                group_id INTEGER,
                group_title TEXT,
                group_chat_id INTEGER,
                message_preview TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_send_log_created ON send_log(created_at DESC);

            CREATE TABLE IF NOT EXISTS operators (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                full_name TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL
            );

            -- multi log groups (pool)
            CREATE TABLE IF NOT EXISTS log_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );

            -- which target groups each mailing account uses (client isolation)
            CREATE TABLE IF NOT EXISTS account_groups (
                account_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (account_id, group_id)
            );

            -- which log groups receive events for this account
            CREATE TABLE IF NOT EXISTS account_log_groups (
                account_id INTEGER NOT NULL,
                log_group_id INTEGER NOT NULL,
                PRIMARY KEY (account_id, log_group_id)
            );

            CREATE TABLE IF NOT EXISTS account_group_joins (
                account_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_attempt_at REAL,
                joined_at REAL,
                last_error TEXT,
                PRIMARY KEY (account_id, group_id)
            );
            """
        )
        await self.db.commit()
        # per-account message + params (nullable = use global defaults)
        await self._ensure_column("accounts", "message_text", "TEXT NOT NULL DEFAULT ''")
        await self._ensure_column("accounts", "cycle_limit", "INTEGER")
        await self._ensure_column("accounts", "cycle_pause_sec", "INTEGER")
        await self._ensure_column("accounts", "delay_sec", "REAL")
        await self._ensure_column("accounts", "added_by", "INTEGER")
        await self._ensure_column("accounts", "client_label", "TEXT NOT NULL DEFAULT ''")
        await self._ensure_column("accounts", "mailing_duration_sec", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("accounts", "mailing_ends_at", "REAL")
        # seed defaults
        defaults = {
            "mailing_enabled": "0",
            "mailing_ends_at": "",  # unix ts; empty = unlimited / no deadline
            "mailing_duration_sec": "0",  # default term on start: 0 = unlimited
            "log_group_id": "",
            "cycle_limit": "50",
            "cycle_pause_sec": "3600",
            "delay_sec": "8",
            "join_pause_sec": "60",
            "active_message_id": "",
        }
        for k, v in defaults.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )
        # ensure one message template
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM messages")
        row = await cur.fetchone()
        if row and row["c"] == 0:
            now = time.time()
            await self.db.execute(
                "INSERT INTO messages (title, text, active, updated_at, created_at) VALUES (?, ?, 1, ?, ?)",
                ("default", "Hello! 👋", now, now),
            )
        await self.db.commit()
        await self._migrate_multi_tenant()

    async def _migrate_multi_tenant(self) -> None:
        """One-time: legacy single log_group + shared groups → multi-tenant tables."""
        # legacy log_group_id → log_groups pool
        legacy = (await self.get_setting("log_group_id", "")).strip()
        if legacy.lstrip("-").isdigit():
            chat_id = int(legacy)
            cur = await self.db.execute(
                "SELECT id FROM log_groups WHERE chat_id = ?", (chat_id,)
            )
            if not await cur.fetchone():
                await self.db.execute(
                    "INSERT INTO log_groups (chat_id, title, active, created_at) VALUES (?, ?, 1, ?)",
                    (chat_id, f"log {chat_id}", time.time()),
                )
                await self.db.commit()

        # if no account_groups links yet, attach all groups to all accounts (keep old behavior once)
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM account_groups")
        row = await cur.fetchone()
        if row and int(row["c"]) == 0:
            accounts = await self.list_accounts()
            groups = await self.list_groups(only_active=False)
            for a in accounts:
                for g in groups:
                    await self.db.execute(
                        "INSERT OR IGNORE INTO account_groups (account_id, group_id) VALUES (?, ?)",
                        (a["id"], g["id"]),
                    )
            await self.db.commit()

        # Legacy/global pool groups may have been added after account_groups
        # already contained some links. Attach only completely unlinked groups.
        # Existing account-specific links remain unchanged.
        await self.db.execute(
            "INSERT OR IGNORE INTO account_groups (account_id, group_id) "
            "SELECT a.id, g.id FROM accounts a CROSS JOIN groups g "
            "WHERE a.status NOT IN ('disabled', 'error', 'expired') "
            "AND g.active = 1 "
            "AND NOT EXISTS ("
            "SELECT 1 FROM account_groups existing "
            "WHERE existing.group_id = g.id"
            ")"
        )
        await self.db.commit()

        # if no account_log_groups, attach all log_groups to all accounts
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM account_log_groups")
        row = await cur.fetchone()
        if row and int(row["c"]) == 0:
            accounts = await self.list_accounts()
            logs = await self.list_log_groups()
            for a in accounts:
                for lg in logs:
                    await self.db.execute(
                        "INSERT OR IGNORE INTO account_log_groups (account_id, log_group_id) VALUES (?, ?)",
                        (a["id"], lg["id"]),
                    )
            await self.db.commit()

    async def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cur = await self.db.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cur.fetchall()}
        if column not in cols:
            await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            await self.db.commit()

    # ── settings ──────────────────────────────────────────────

    async def get_setting(self, key: str, default: str = "") -> str:
        cur = await self.db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        await self.db.commit()

    async def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(await self.get_setting(key, str(default)))
        except ValueError:
            return default

    async def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(await self.get_setting(key, str(default)))
        except ValueError:
            return default

    async def is_mailing_enabled(self) -> bool:
        return (await self.get_setting("mailing_enabled", "0")) in ("1", "true", "yes")

    async def set_mailing_enabled(self, enabled: bool) -> None:
        await self.set_setting("mailing_enabled", "1" if enabled else "0")
        if not enabled:
            await self.set_setting("mailing_ends_at", "")

    async def start_mailing(self, duration_sec: int | None = None) -> None:
        """Enable mailing. duration_sec=None/0 → no deadline; else auto-stop after N seconds."""
        await self.set_setting("mailing_enabled", "1")
        if duration_sec and duration_sec > 0:
            await self.set_setting("mailing_ends_at", str(time.time() + int(duration_sec)))
        else:
            await self.set_setting("mailing_ends_at", "")
        # remember last/default choice for settings UI
        await self.set_setting(
            "mailing_duration_sec",
            str(int(duration_sec) if duration_sec and duration_sec > 0 else 0),
        )

    async def get_mailing_duration_default(self) -> int:
        """Preferred duration in seconds (0 = unlimited)."""
        return max(0, await self.get_int("mailing_duration_sec", 0))

    async def set_mailing_duration_default(self, duration_sec: int) -> None:
        """Save default term; if mailing is on — reset the active deadline from now."""
        sec = max(0, int(duration_sec))
        await self.set_setting("mailing_duration_sec", str(sec))
        if await self.is_mailing_enabled():
            if sec > 0:
                await self.set_setting("mailing_ends_at", str(time.time() + sec))
            else:
                await self.set_setting("mailing_ends_at", "")

    async def get_mailing_ends_at(self) -> float | None:
        raw = (await self.get_setting("mailing_ends_at", "")).strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    async def mailing_time_left(self) -> float | None:
        """Seconds until auto-stop, None if unlimited, 0 if already expired."""
        ends = await self.get_mailing_ends_at()
        if ends is None:
            return None
        return max(0.0, ends - time.time())

    async def check_and_expire_mailing(self) -> bool:
        """If deadline passed — disable mailing. Returns True when just expired."""
        if not await self.is_mailing_enabled():
            return False
        ends = await self.get_mailing_ends_at()
        if ends is None:
            return False
        if time.time() >= ends:
            await self.set_mailing_enabled(False)
            return True
        return False

    # ── accounts ──────────────────────────────────────────────

    async def add_account(
        self,
        phone: str,
        session_name: str,
        label: str = "",
        added_by: int | None = None,
    ) -> int:
        now = time.time()
        cur = await self.db.execute(
            "INSERT INTO accounts (phone, label, session_name, status, created_at, added_by) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (phone, label or phone, session_name, now, added_by),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    # ── operators (team members who may use the bot) ───────────

    async def upsert_operator(
        self, user_id: int, username: str = "", full_name: str = ""
    ) -> None:
        await self.db.execute(
            "INSERT INTO operators (user_id, username, full_name, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
            "full_name = excluded.full_name",
            (user_id, username or "", full_name or "", time.time()),
        )
        await self.db.commit()

    async def remove_operator(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM operators WHERE user_id = ?", (user_id,))
        await self.db.commit()

    async def list_operators(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM operators ORDER BY added_at")
        return [dict(r) for r in await cur.fetchall()]

    async def is_operator(self, user_id: int) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM operators WHERE user_id = ?", (user_id,)
        )
        return (await cur.fetchone()) is not None

    async def list_accounts(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM accounts ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_account_by_phone(self, phone: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM accounts WHERE phone = ?", (phone,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_account_status(self, account_id: int, status: str, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE accounts SET status = ?, last_error = ? WHERE id = ?",
            (status, error, account_id),
        )
        await self.db.commit()

    async def delete_account(self, account_id: int) -> None:
        await self.db.execute("DELETE FROM account_groups WHERE account_id = ?", (account_id,))
        await self.db.execute("DELETE FROM account_log_groups WHERE account_id = ?", (account_id,))
        await self.db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        await self.db.commit()

    async def set_client_label(self, account_id: int, label: str) -> None:
        await self.db.execute(
            "UPDATE accounts SET client_label = ? WHERE id = ?",
            ((label or "").strip()[:64], account_id),
        )
        await self.db.commit()

    async def set_account_message(self, account_id: int, text: str) -> None:
        await self.db.execute(
            "UPDATE accounts SET message_text = ? WHERE id = ?",
            (text or "", account_id),
        )
        await self.db.commit()

    async def set_account_param(
        self,
        account_id: int,
        *,
        cycle_limit: int | None = ...,  # type: ignore[assignment]
        cycle_pause_sec: int | None = ...,  # type: ignore[assignment]
        delay_sec: float | None = ...,  # type: ignore[assignment]
    ) -> None:
        """Update per-account params. Pass None to reset to global. Omit to leave unchanged."""
        acc = await self.get_account(account_id)
        if not acc:
            return
        fields: list[str] = []
        vals: list[Any] = []
        if cycle_limit is not ...:
            fields.append("cycle_limit = ?")
            vals.append(cycle_limit)
        if cycle_pause_sec is not ...:
            fields.append("cycle_pause_sec = ?")
            vals.append(cycle_pause_sec)
        if delay_sec is not ...:
            fields.append("delay_sec = ?")
            vals.append(delay_sec)
        if not fields:
            return
        vals.append(account_id)
        await self.db.execute(
            f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?",
            vals,
        )
        await self.db.commit()

    async def account_message_text(self, account: dict[str, Any]) -> str:
        """Own message if set, otherwise global active template."""
        own = (account.get("message_text") or "").strip()
        if own:
            return own
        msg = await self.get_active_message()
        return ((msg or {}).get("text") or "").strip()

    async def account_cycle_limit(self, account: dict[str, Any]) -> int:
        v = account.get("cycle_limit")
        if v is not None and str(v).strip() != "":
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                pass
        return await self.get_int("cycle_limit", 50)

    async def account_cycle_pause(self, account: dict[str, Any]) -> int:
        v = account.get("cycle_pause_sec")
        if v is not None and str(v).strip() != "":
            try:
                return max(60, int(v))
            except (TypeError, ValueError):
                pass
        return await self.get_int("cycle_pause_sec", 3600)

    async def account_delay(self, account: dict[str, Any]) -> float:
        v = account.get("delay_sec")
        if v is not None and str(v).strip() != "":
            try:
                return max(1.0, float(v))
            except (TypeError, ValueError):
                pass
        return await self.get_float("delay_sec", 8.0)

    async def account_duration(self, account: dict[str, Any]) -> int:
        """Per-account duration in seconds; 0 means unlimited."""
        try:
            return max(0, int(account.get("mailing_duration_sec") or 0))
        except (TypeError, ValueError):
            return 0

    async def account_time_left(self, account: dict[str, Any]) -> float | None:
        ends = account.get("mailing_ends_at")
        if ends is None or str(ends).strip() == "":
            return None
        try:
            return max(0.0, float(ends) - time.time())
        except (TypeError, ValueError):
            return None

    async def account_duration_expired(self, account: dict[str, Any]) -> bool:
        ends = account.get("mailing_ends_at")
        if ends is None or str(ends).strip() == "":
            return False
        try:
            return time.time() >= float(ends)
        except (TypeError, ValueError):
            return False

    async def set_account_duration(self, account_id: int, duration_sec: int) -> None:
        sec = max(0, int(duration_sec))
        ends = time.time() + sec if sec > 0 else None
        await self.db.execute(
            "UPDATE accounts SET mailing_duration_sec = ?, mailing_ends_at = ?, "
            "status = CASE WHEN status = 'expired' THEN 'active' ELSE status END "
            "WHERE id = ?",
            (sec, ends, account_id),
        )
        await self.db.commit()

    async def mark_sent(self, account_id: int) -> dict[str, Any]:
        """Increment cycle counter; if limit reached — schedule next cycle."""
        acc = await self.get_account(account_id)
        if not acc:
            return {}
        cycle_limit = await self.account_cycle_limit(acc)
        pause = await self.account_cycle_pause(acc)
        now = time.time()
        sent = int(acc["sent_in_cycle"] or 0) + 1
        cycle_started = acc["cycle_started_at"] or now

        if sent >= cycle_limit:
            next_cycle_at = now + pause
            # keep sent_in_cycle at limit for display until cooldown ends
            await self.db.execute(
                "UPDATE accounts SET sent_in_cycle = ?, cycle_started_at = NULL, "
                "next_cycle_at = ?, last_sent_at = ?, status = 'cooldown' WHERE id = ?",
                (cycle_limit, next_cycle_at, now, account_id),
            )
            await self.db.commit()
            return (await self.get_account(account_id)) or {}

        await self.db.execute(
            "UPDATE accounts SET sent_in_cycle = ?, cycle_started_at = ?, "
            "next_cycle_at = NULL, last_sent_at = ?, status = 'active' WHERE id = ?",
            (sent, cycle_started, now, account_id),
        )
        await self.db.commit()
        return (await self.get_account(account_id)) or {}

    async def maybe_end_cooldown(self, account_id: int) -> bool:
        """If cooldown finished — start new cycle. Returns True if account is ready to send."""
        acc = await self.get_account(account_id)
        if not acc:
            return False
        if acc["status"] == "disabled":
            return False
        now = time.time()
        if acc["status"] == "cooldown":
            next_at = acc["next_cycle_at"] or 0
            if now >= next_at:
                await self.db.execute(
                    "UPDATE accounts SET status = 'active', sent_in_cycle = 0, "
                    "cycle_started_at = ?, next_cycle_at = NULL, last_error = NULL WHERE id = ?",
                    (now, account_id),
                )
                await self.db.commit()
                return True
            return False
        if acc["status"] == "active":
            if not acc["cycle_started_at"]:
                await self.db.execute(
                    "UPDATE accounts SET cycle_started_at = ? WHERE id = ?",
                    (now, account_id),
                )
                await self.db.commit()
            return True
        return False

    # ── groups ────────────────────────────────────────────────

    async def add_group(self, chat_id: int, title: str = "", username: str = "") -> int:
        now = time.time()
        await self.db.execute(
            "INSERT INTO groups (chat_id, title, username, active, created_at) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, username = excluded.username, active = 1",
            (chat_id, title or str(chat_id), username or "", now),
        )
        await self.db.commit()
        cur = await self.db.execute("SELECT id FROM groups WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        return int(row["id"]) if row else 0

    async def list_groups(self, only_active: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM groups"
        if only_active:
            q += " WHERE active = 1"
        q += " ORDER BY id"
        cur = await self.db.execute(q)
        return [dict(r) for r in await cur.fetchall()]

    async def get_group(self, group_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_group_active(self, group_id: int, active: bool) -> None:
        await self.db.execute(
            "UPDATE groups SET active = ? WHERE id = ?",
            (1 if active else 0, group_id),
        )
        await self.db.commit()

    async def delete_group(self, group_id: int) -> None:
        await self.db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        await self.db.commit()

    async def touch_group_sent(self, group_id: int) -> None:
        await self.db.execute(
            "UPDATE groups SET last_sent_at = ? WHERE id = ?",
            (time.time(), group_id),
        )
        await self.db.commit()

    # ── account ↔ target groups (per-client isolation) ────────

    async def list_account_groups(self, account_id: int, only_active: bool = True) -> list[dict[str, Any]]:
        q = (
            "SELECT g.* FROM groups g "
            "INNER JOIN account_groups ag ON ag.group_id = g.id "
            "WHERE ag.account_id = ?"
        )
        if only_active:
            q += " AND g.active = 1"
        q += " ORDER BY g.id"
        cur = await self.db.execute(q, (account_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def link_account_group(self, account_id: int, group_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO account_groups (account_id, group_id) VALUES (?, ?)",
            (account_id, group_id),
        )
        await self.db.commit()

    async def unlink_account_group(self, account_id: int, group_id: int) -> None:
        await self.db.execute(
            "DELETE FROM account_groups WHERE account_id = ? AND group_id = ?",
            (account_id, group_id),
        )
        await self.db.commit()

    async def account_has_group(self, account_id: int, group_id: int) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM account_groups WHERE account_id = ? AND group_id = ?",
            (account_id, group_id),
        )
        return (await cur.fetchone()) is not None

    async def list_group_join_queue(self, now: float, limit: int = 1) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT a.*, g.id AS group_id, g.chat_id, g.title AS group_title, "
            "g.username AS group_username, agj.status AS join_status "
            "FROM account_groups ag "
            "JOIN accounts a ON a.id = ag.account_id "
            "JOIN groups g ON g.id = ag.group_id "
            "LEFT JOIN account_group_joins agj "
            "ON agj.account_id = ag.account_id AND agj.group_id = ag.group_id "
            "WHERE a.status NOT IN ('disabled', 'error', 'expired') "
            "AND g.active = 1 "
            "AND COALESCE(agj.status, 'queued') NOT IN ('joined', 'permanent_error') "
            "AND COALESCE(agj.next_attempt_at, 0) <= ? "
            "ORDER BY COALESCE(agj.next_attempt_at, 0), a.id, g.id LIMIT ?",
            (now, max(1, int(limit))),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def record_group_join(
        self, account_id: int, group_id: int, *, status: str,
        next_attempt_at: float = 0, error: str | None = None,
    ) -> None:
        now = time.time()
        joined_at = now if status == "joined" else None
        await self.db.execute(
            "INSERT INTO account_group_joins "
            "(account_id, group_id, status, next_attempt_at, last_attempt_at, joined_at, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, group_id) DO UPDATE SET "
            "status=excluded.status, next_attempt_at=excluded.next_attempt_at, "
            "last_attempt_at=excluded.last_attempt_at, "
            "joined_at=COALESCE(excluded.joined_at, account_group_joins.joined_at), "
            "last_error=excluded.last_error",
            (account_id, group_id, status, float(next_attempt_at), now, joined_at, error),
        )
        await self.db.commit()

    # ── log groups (multi) ────────────────────────────────────

    async def add_log_group(self, chat_id: int, title: str = "") -> int:
        now = time.time()
        await self.db.execute(
            "INSERT INTO log_groups (chat_id, title, active, created_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, active = 1",
            (chat_id, title or str(chat_id), now),
        )
        await self.db.commit()
        cur = await self.db.execute("SELECT id FROM log_groups WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        return int(row["id"]) if row else 0

    async def list_log_groups(self, only_active: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM log_groups"
        if only_active:
            q += " WHERE active = 1"
        q += " ORDER BY id"
        cur = await self.db.execute(q)
        return [dict(r) for r in await cur.fetchall()]

    async def get_log_group(self, log_group_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM log_groups WHERE id = ?", (log_group_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_log_group_active(self, log_group_id: int, active: bool) -> None:
        await self.db.execute(
            "UPDATE log_groups SET active = ? WHERE id = ?",
            (1 if active else 0, log_group_id),
        )
        await self.db.commit()

    async def delete_log_group(self, log_group_id: int) -> None:
        await self.db.execute(
            "DELETE FROM account_log_groups WHERE log_group_id = ?", (log_group_id,)
        )
        await self.db.execute("DELETE FROM log_groups WHERE id = ?", (log_group_id,))
        await self.db.commit()

    async def list_account_log_groups(self, account_id: int) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT lg.* FROM log_groups lg "
            "INNER JOIN account_log_groups alg ON alg.log_group_id = lg.id "
            "WHERE alg.account_id = ? AND lg.active = 1 ORDER BY lg.id",
            (account_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def link_account_log_group(self, account_id: int, log_group_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO account_log_groups (account_id, log_group_id) VALUES (?, ?)",
            (account_id, log_group_id),
        )
        await self.db.commit()

    async def unlink_account_log_group(self, account_id: int, log_group_id: int) -> None:
        await self.db.execute(
            "DELETE FROM account_log_groups WHERE account_id = ? AND log_group_id = ?",
            (account_id, log_group_id),
        )
        await self.db.commit()

    async def account_log_chat_ids(self, account_id: int) -> list[int]:
        """Chat IDs to notify for this account (multi log groups)."""
        logs = await self.list_account_log_groups(account_id)
        ids = [int(x["chat_id"]) for x in logs]
        if ids:
            return ids
        # legacy single setting fallback
        legacy = (await self.get_setting("log_group_id", "")).strip()
        if legacy.lstrip("-").isdigit():
            return [int(legacy)]
        return []

    # ── messages ──────────────────────────────────────────────

    async def list_messages(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM messages ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def get_message(self, message_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_active_message(self) -> dict[str, Any] | None:
        mid = await self.get_setting("active_message_id", "")
        if mid.isdigit():
            msg = await self.get_message(int(mid))
            if msg and msg["active"]:
                return msg
        cur = await self.db.execute(
            "SELECT * FROM messages WHERE active = 1 ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_message_text(self, message_id: int, text: str) -> None:
        await self.db.execute(
            "UPDATE messages SET text = ?, updated_at = ? WHERE id = ?",
            (text, time.time(), message_id),
        )
        await self.db.commit()

    async def add_message(self, title: str, text: str) -> int:
        now = time.time()
        cur = await self.db.execute(
            "INSERT INTO messages (title, text, active, updated_at, created_at) VALUES (?, ?, 1, ?, ?)",
            (title, text, now, now),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def set_active_message(self, message_id: int) -> None:
        await self.set_setting("active_message_id", str(message_id))

    # ── send log ──────────────────────────────────────────────

    async def log_send(
        self,
        *,
        account_id: int | None,
        group_id: int | None,
        group_title: str,
        group_chat_id: int | None,
        message_preview: str,
        status: str,
        error: str | None = None,
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO send_log (account_id, group_id, group_title, group_chat_id, "
            "message_preview, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                group_id,
                group_title,
                group_chat_id,
                (message_preview or "")[:200],
                status,
                error,
                time.time(),
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def recent_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM send_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def stats(self) -> dict[str, Any]:
        async def count(sql: str) -> int:
            cur = await self.db.execute(sql)
            row = await cur.fetchone()
            return int(row[0]) if row else 0

        return {
            "accounts": await count("SELECT COUNT(*) FROM accounts"),
            "accounts_active": await count("SELECT COUNT(*) FROM accounts WHERE status = 'active'"),
            "accounts_cooldown": await count("SELECT COUNT(*) FROM accounts WHERE status = 'cooldown'"),
            "groups": await count("SELECT COUNT(*) FROM groups WHERE active = 1"),
            "sends_ok": await count("SELECT COUNT(*) FROM send_log WHERE status = 'ok'"),
            "sends_fail": await count("SELECT COUNT(*) FROM send_log WHERE status != 'ok'"),
            "mailing": await self.is_mailing_enabled(),
        }
