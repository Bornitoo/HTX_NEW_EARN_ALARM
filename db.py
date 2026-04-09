import aiosqlite
import os
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "htx-earn.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS earn_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scraped_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS earn_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER REFERENCES earn_cycles(id),
                token TEXT,
                apy TEXT,
                term TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()


async def get_setting(key: str, default: str = None) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()


async def save_cycle(rows: list[dict]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO earn_cycles DEFAULT VALUES")
        cycle_id = cur.lastrowid
        for r in rows:
            await db.execute(
                "INSERT INTO earn_rows (cycle_id, token, apy, term) VALUES (?,?,?,?)",
                (cycle_id, r["token"], r["apy"], r["term"])
            )
        await db.commit()
        return cycle_id


async def get_last_cycle_rows() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM earn_cycles ORDER BY id DESC LIMIT 1 OFFSET 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return []
        cycle_id = row[0]
        async with db.execute(
            "SELECT token, apy, term FROM earn_rows WHERE cycle_id=?",
            (cycle_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [{"token": r[0], "apy": r[1], "term": r[2]} for r in rows]


async def get_current_cycle_rows() -> list[dict]:
    """Returns rows from the most recent cycle."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM earn_cycles ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return []
        cycle_id = row[0]
        async with db.execute(
            "SELECT token, apy, term FROM earn_rows WHERE cycle_id=?",
            (cycle_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [{"token": r[0], "apy": r[1], "term": r[2]} for r in rows]


def compute_diff(old_rows: list[dict], new_rows: list[dict]):
    old = {(r["token"], r["term"]): r["apy"] for r in old_rows}
    new = {(r["token"], r["term"]): r["apy"] for r in new_rows}

    added = [(t, d, apy) for (t, d), apy in new.items() if (t, d) not in old]
    removed = [(t, d, apy) for (t, d), apy in old.items() if (t, d) not in new]
    changed = [
        (t, d, old[(t, d)], apy)
        for (t, d), apy in new.items()
        if (t, d) in old and old[(t, d)] != apy
    ]
    return added, removed, changed


def format_diff(added, removed, changed) -> str:
    lines = ["⚠️ <b>HTX Earn — изменения:</b>"]
    for token, term, apy in added:
        lines.append(f"🟢 НОВОЕ: <b>{token}</b> {apy} Fixed {term}")
    for token, term, apy in removed:
        lines.append(f"🔴 ПРОПАЛО: <b>{token}</b> {apy} Fixed {term}")
    for token, term, old_apy, new_apy in changed:
        lines.append(f"🔄 ИЗМЕНИЛОСЬ: <b>{token}</b> {old_apy}→{new_apy} Fixed {term}")
    return "\n".join(lines)


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "Нет данных Fixed."
    lines = ["<b>HTX Earn — Fixed:</b>", "<pre>"]
    lines.append(f"{'Токен':<12} {'APY':>8}  {'Срок'}")
    lines.append("-" * 32)
    for r in rows:
        lines.append(f"{r['token']:<12} {r['apy']:>8}  {r['term']}")
    lines.append("</pre>")
    return "\n".join(lines)


async def schedule_next_run(interval_minutes: int):
    next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
    await set_setting("next_run_at", next_run)
