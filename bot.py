import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
from pathlib import Path
from typing import Optional

from telegram import InputMediaDocument, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DB_PATH = Path(os.getenv("DB_PATH", "files.db"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


@dataclass
class ShareLink:
    code: str
    owner_id: int
    owner_username: str
    link_type: str
    title: str
    private_user_id: Optional[int]
    expires_at: Optional[str]


class FileStore:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS share_links (
                code TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                owner_username TEXT,
                link_type TEXT NOT NULL CHECK(link_type IN ('file','album')),
                title TEXT NOT NULL,
                private_user_id INTEGER,
                expires_at TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                download_count INTEGER DEFAULT 0,
                last_downloaded_at TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS share_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                item_order INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                mime_type TEXT,
                file_size INTEGER,
                FOREIGN KEY(code) REFERENCES share_links(code) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                downloader_id INTEGER NOT NULL,
                downloaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(code) REFERENCES share_links(code) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.commit()

    def add_link(self, link: ShareLink, items: list[dict]) -> None:
        self.conn.execute(
            """
            INSERT INTO share_links (code, owner_id, owner_username, link_type, title, private_user_id, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link.code,
                link.owner_id,
                link.owner_username,
                link.link_type,
                link.title,
                link.private_user_id,
                link.expires_at,
            ),
        )
        self.conn.executemany(
            """
            INSERT INTO share_items (code, item_order, file_id, file_unique_id, file_name, mime_type, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    link.code,
                    i,
                    item["file_id"],
                    item["file_unique_id"],
                    item["file_name"],
                    item["mime_type"],
                    item["file_size"],
                )
                for i, item in enumerate(items)
            ],
        )
        self.conn.commit()

    def get_link(self, code: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM share_links WHERE code = ?", (code,)
        ).fetchone()

    def get_items(self, code: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM share_items WHERE code = ? ORDER BY item_order ASC", (code,)
        ).fetchall()

    def list_owner_links(self, owner_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM share_links
            WHERE owner_id = ?
            ORDER BY created_at DESC
            """,
            (owner_id,),
        ).fetchall()

    def delete_owner_link(self, owner_id: int, code: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM share_links WHERE owner_id = ? AND code = ?", (owner_id, code)
        )
        self.conn.commit()
        return cur.rowcount

    def record_download(self, code: str, downloader_id: int) -> None:
        self.conn.execute(
            "INSERT INTO downloads (code, downloader_id) VALUES (?, ?)", (code, downloader_id)
        )
        self.conn.execute(
            """
            UPDATE share_links
            SET download_count = download_count + 1,
                last_downloaded_at = CURRENT_TIMESTAMP
            WHERE code = ?
            """,
            (code,),
        )
        self.conn.commit()

    def owner_analytics(self, owner_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT l.code, l.title, l.link_type, l.download_count,
                   COUNT(DISTINCT d.downloader_id) AS unique_downloaders,
                   l.last_downloaded_at
            FROM share_links l
            LEFT JOIN downloads d ON d.code = l.code
            WHERE l.owner_id = ?
            GROUP BY l.code, l.title, l.link_type, l.download_count, l.last_downloaded_at
            ORDER BY l.download_count DESC, l.created_at DESC
            """,
            (owner_id,),
        ).fetchall()

    def admin_summary(self) -> sqlite3.Row:
        return self.conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM share_links) AS total_links,
              (SELECT COUNT(*) FROM share_items) AS total_items,
              (SELECT COUNT(*) FROM downloads) AS total_downloads,
              (SELECT COUNT(DISTINCT owner_id) FROM share_links) AS active_uploaders
            """
        ).fetchone()


store = FileStore(DB_PATH)


def gen_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]


def build_share_link(code: str) -> str:
    if not BOT_USERNAME:
        return f"(set BOT_USERNAME to generate links) code: {code}"
    return f"https://t.me/{BOT_USERNAME}?start=share_{code}"


def parse_expiry(when: Optional[str]) -> Optional[datetime]:
    if not when:
        return None
    return datetime.fromisoformat(when)


def fmt_link_row(row: sqlite3.Row) -> str:
    exp = f" | expires: {row['expires_at']}" if row["expires_at"] else ""
    prv = f" | private:{row['private_user_id']}" if row["private_user_id"] else ""
    return f"- `{row['code']}` | {row['link_type']} | {row['title']}{prv}{exp}\n  {build_share_link(row['code'])}"


def set_user_draft(context: ContextTypes.DEFAULT_TYPE, *, private_user_id: Optional[int], expires_minutes: Optional[int]) -> None:
    context.user_data["draft_private_user_id"] = private_user_id
    context.user_data["draft_expires_minutes"] = expires_minutes


def get_expiry_from_context(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    minutes = context.user_data.get("draft_expires_minutes")
    if minutes is None:
        return None
    expiry = datetime.now(timezone.utc) + timedelta(minutes=int(minutes))
    return expiry.replace(tzinfo=None).isoformat(timespec="seconds")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0].startswith("share_"):
        code = context.args[0].split("share_", 1)[1]
        link = store.get_link(code)
        if not link:
            await update.message.reply_text("Link is invalid.")
            return

        expires_at = parse_expiry(link["expires_at"])
        if expires_at and datetime.utcnow() > expires_at:
            await update.message.reply_text("This link has expired.")
            return

        if link["private_user_id"] and update.effective_user.id != link["private_user_id"]:
            await update.message.reply_text("This is a private link. You are not authorized.")
            return

        items = store.get_items(code)
        if not items:
            await update.message.reply_text("No files found for this link.")
            return

        if link["link_type"] == "album" and len(items) > 1:
            media = [
                InputMediaDocument(media=item["file_id"], caption=link["title"] if i == 0 else None)
                for i, item in enumerate(items[:10])
            ]
            await context.bot.send_media_group(chat_id=update.effective_chat.id, media=media)
            for extra in items[10:]:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=extra["file_id"])
        else:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=items[0]["file_id"],
                caption=f"Shared file: `{items[0]['file_name']}`",
                parse_mode=ParseMode.MARKDOWN,
            )

        store.record_download(code, update.effective_user.id)
        return

    await update.message.reply_text(
        "Send me files/documents and I will create share links.\n\n"
        "Commands:\n"
        "/list - your links\n"
        "/delete <code> - delete one link\n"
        "/analytics - your download stats\n"
        "/private <user_id|off> - set next upload private\n"
        "/expiry <minutes|off> - set next upload expiry\n"
        "/help - help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def private_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /private <telegram_user_id|off>")
        return

    arg = context.args[0].strip().lower()
    if arg == "off":
        set_user_draft(context, private_user_id=None, expires_minutes=context.user_data.get("draft_expires_minutes"))
        await update.message.reply_text("Private mode disabled for next uploads.")
        return

    if not arg.isdigit():
        await update.message.reply_text("User ID must be numeric, e.g. /private 123456789")
        return

    set_user_draft(
        context,
        private_user_id=int(arg),
        expires_minutes=context.user_data.get("draft_expires_minutes"),
    )
    await update.message.reply_text(f"Next uploads will be private to user ID `{arg}`.", parse_mode=ParseMode.MARKDOWN)


async def expiry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /expiry <minutes|off>")
        return

    arg = context.args[0].strip().lower()
    if arg == "off":
        set_user_draft(context, private_user_id=context.user_data.get("draft_private_user_id"), expires_minutes=None)
        await update.message.reply_text("Expiry disabled for next uploads.")
        return

    if not arg.isdigit() or int(arg) <= 0:
        await update.message.reply_text("Minutes must be a positive number, e.g. /expiry 60")
        return

    set_user_draft(
        context,
        private_user_id=context.user_data.get("draft_private_user_id"),
        expires_minutes=int(arg),
    )
    await update.message.reply_text(f"Next uploads will expire in `{arg}` minutes.", parse_mode=ParseMode.MARKDOWN)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a file/document.")
        return

    code = None
    for _ in range(8):
        candidate = gen_code()
        if not store.get_link(candidate):
            code = candidate
            break
    if not code:
        await update.message.reply_text("Could not generate unique link. Try again.")
        return

    owner = update.effective_user
    expires_at = get_expiry_from_context(context)
    private_user_id = context.user_data.get("draft_private_user_id")

    link = ShareLink(
        code=code,
        owner_id=owner.id,
        owner_username=owner.username or "",
        link_type="file",
        title=doc.file_name or "unnamed",
        private_user_id=private_user_id,
        expires_at=expires_at,
    )
    item = {
        "file_id": doc.file_id,
        "file_unique_id": doc.file_unique_id,
        "file_name": doc.file_name or "unnamed",
        "mime_type": doc.mime_type or "",
        "file_size": doc.file_size or 0,
    }
    store.add_link(link, [item])

    summary = []
    if private_user_id:
        summary.append(f"private to `{private_user_id}`")
    if expires_at:
        summary.append(f"expires `{expires_at}` UTC")
    extra = f"\nOptions: {', '.join(summary)}" if summary else ""

    await update.message.reply_text(
        f"Saved ✅\nCode: `{code}`\nShare URL:\n{build_share_link(code)}{extra}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_album(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message = update.message
    if not msg or not msg.media_group_id or not msg.document:
        return

    key = f"album_{msg.media_group_id}"
    bucket = context.bot_data.setdefault(key, [])
    bucket.append(msg)

    if len(bucket) == 1:
        context.application.create_task(finalize_album(update, context, msg.media_group_id))


async def finalize_album(update: Update, context: ContextTypes.DEFAULT_TYPE, media_group_id: str) -> None:
    await asyncio.sleep(1.2)
    key = f"album_{media_group_id}"
    bucket: list[Message] = context.bot_data.pop(key, [])
    if len(bucket) < 2:
        return

    bucket.sort(key=lambda m: m.message_id)
    code = None
    for _ in range(8):
        candidate = gen_code()
        if not store.get_link(candidate):
            code = candidate
            break
    if not code:
        await bucket[0].reply_text("Could not generate album link. Try again.")
        return

    owner = bucket[0].from_user
    expires_at = get_expiry_from_context(context)
    private_user_id = context.user_data.get("draft_private_user_id")
    items = [
        {
            "file_id": m.document.file_id,
            "file_unique_id": m.document.file_unique_id,
            "file_name": m.document.file_name or f"file_{i+1}",
            "mime_type": m.document.mime_type or "",
            "file_size": m.document.file_size or 0,
        }
        for i, m in enumerate(bucket)
    ]
    title = f"Album ({len(items)} files)"
    link = ShareLink(
        code=code,
        owner_id=owner.id,
        owner_username=owner.username or "",
        link_type="album",
        title=title,
        private_user_id=private_user_id,
        expires_at=expires_at,
    )
    store.add_link(link, items)

    await bucket[-1].reply_text(
        f"Album saved ✅\nCode: `{code}`\nShare URL:\n{build_share_link(code)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = store.list_owner_links(update.effective_user.id)
    if not rows:
        await update.message.reply_text("You have no links yet. Send files first.")
        return

    lines = ["Your links:"]
    for row in rows[:30]:
        lines.append(fmt_link_row(row))
    if len(rows) > 30:
        lines.append(f"... and {len(rows) - 30} more")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /delete <code>")
        return

    code = context.args[0].strip()
    deleted = store.delete_owner_link(update.effective_user.id, code)
    if deleted:
        await update.message.reply_text(f"Deleted link `{code}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No link found with that code in your uploads.")


async def analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = store.owner_analytics(update.effective_user.id)
    if not rows:
        await update.message.reply_text("No analytics yet. Upload and share files first.")
        return

    lines = ["Download analytics (top links):"]
    for row in rows[:20]:
        lines.append(
            f"- `{row['code']}` {row['title']} | downloads: {row['download_count']} | unique: {row['unique_downloaders']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("You are not an admin.")
        return

    summary = store.admin_summary()
    await update.message.reply_text(
        "Admin panel\n"
        f"Total links: {summary['total_links']}\n"
        f"Total file items: {summary['total_items']}\n"
        f"Total downloads: {summary['total_downloads']}\n"
        f"Active uploaders: {summary['active_uploaders']}"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN environment variable")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("private", private_cmd))
    app.add_handler(CommandHandler("expiry", expiry_cmd))
    app.add_handler(CommandHandler("analytics", analytics_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.UpdateType.MESSAGE, handle_album), group=0)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document), group=1)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
