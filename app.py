"""
FlyCord — a tiny VK/Twitter-style global broadcast feed built with Flask + sqlite3.

Run locally:
    pip install -r requirements.txt
    python app.py

Then expose it with ngrok if you want other people to reach it:
    ngrok http 5000

Everyone who hits the ngrok URL and registers an account shares the SAME
sqlite database (flycord.db), so it behaves like one global feed / user
directory — exactly like the screenshot.
"""

import base64
import json
import os
import random
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, g, render_template, request, redirect, url_for,
    session, flash, jsonify, abort, send_from_directory
)
from markupsafe import escape, Markup
from werkzeug.security import generate_password_hash, check_password_hash
from py_vapid import Vapid
from pywebpush import webpush, WebPushException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "flycord.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
BROADCAST_MAX_LEN = 400
MESSAGE_MAX_LEN = 500
MIN_PASSWORD_LEN = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_VIDEO_BYTES = 25 * 1024 * 1024  # 25 MB

# Allowed image types: extension -> the real file signature ("magic bytes")
# it must start with. We check the actual file content, not just the
# filename/extension the browser reports — those are trivial to fake.
IMAGE_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}

# No more emoji avatars — everyone gets an uploaded picture or this plain
# generic guest icon (a simple person silhouette, drawn as inline SVG so
# there's no emoji involved at all). Anyone who had an emoji avatar from
# before this change now just falls back to this icon automatically, since
# avatar_html() no longer reads the old emoji column for display — see
# avatar_html() below.
GUEST_AVATAR_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<circle cx="12" cy="8" r="4" fill="currentColor"/>'
    '<path d="M4 20c0-4.4 3.6-8 8-8s8 3.6 8 8" fill="currentColor"/>'
    "</svg>"
)

# Usernames that get admin powers — deleting any broadcast (not just their
# own), banning/unbanning other accounts, and granting verified badges.
# Admin accounts themselves can't be banned. This check is intentionally
# CASE-SENSITIVE: only the exact spelling below counts as an admin, so an
# account like "matan" or "MATAN" is just a normal user, never an impostor
# with real admin powers. (Registration also blocks anyone from creating a
# same-letters-different-case username at all — see register().)
ADMIN_USERNAMES = {"Matan"}

# Explicit folders so the app finds templates/static regardless of the
# working directory it's launched from.
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.secret_key = os.environ.get("FLYCORD_SECRET", "dev-secret-change-me")
# Hard cap on request body size so nobody can flood the disk with giant
# uploads. A bit above the bigger of the two upload caps to leave room for
# the rest of the form.
app.config["MAX_CONTENT_LENGTH"] = MAX_VIDEO_BYTES + (256 * 1024)

os.makedirs(UPLOAD_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# Web push notifications (VAPID keys)
#
# The key pair identifies this server to push services (Apple's, Google's,
# Mozilla's, etc.) so they'll trust the push messages it sends. Generated
# once and saved to disk — regenerating it would silently break every
# device's existing subscription, since they were signed for the old key.
# --------------------------------------------------------------------------
VAPID_KEY_PATH = os.path.join(BASE_DIR, "vapid_private_key.pem")
VAPID_CLAIMS_SUB = os.environ.get("FLYCORD_VAPID_SUBJECT", "mailto:admin@example.com")
_vapid = Vapid.from_file(VAPID_KEY_PATH)


def vapid_public_key_b64():
    """The public half of the VAPID key, in the URL-safe base64 raw format
    the browser's PushManager.subscribe() expects as applicationServerKey."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = _vapid.private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def send_push_to_user(user_id, title, body, url):
    """Best-effort — never raises, never blocks whatever called it. A push
    failing (expired subscription, offline device, etc.) shouldn't stop a
    DM from actually sending."""
    db = get_db()
    subs = db.execute(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?", (user_id,)
    ).fetchall()
    if not subs:
        return

    payload = json.dumps({"title": title, "body": body, "url": url})
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_KEY_PATH,
                vapid_claims={"sub": VAPID_CLAIMS_SUB},
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                # Subscription is gone (browser data cleared, uninstalled,
                # etc.) — clean it up so we stop trying.
                db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
                db.commit()
        except Exception:
            pass  # network hiccup, push service down, etc. — not fatal


def wants_json():
    """True if the client is an AJAX call expecting a JSON response."""
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )


@app.errorhandler(413)
def handle_too_large(e):
    msg = (
        f"That file is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)}MB for "
        f"images, {MAX_VIDEO_BYTES // (1024 * 1024)}MB for videos)."
    )
    if wants_json():
        return jsonify({"error": msg}), 413
    flash(msg, "error")
    return redirect(request.referrer or url_for("feed"))


def save_uploaded_image(file_storage):
    """Validate + save an uploaded image. Returns the saved filename, or
    (None, error_message) if the upload was missing/invalid.

    Only PNG and JPEG are accepted, and we don't trust the filename or the
    browser-reported content type — we check the file's actual magic bytes
    so a renamed .exe (etc.) can't sneak through as a ".png".
    """
    if file_storage is None or not file_storage.filename:
        return None, None  # no image submitted — not an error

    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in IMAGE_SIGNATURES:
        return None, "Only PNG and JPG images are allowed."

    header = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    if not any(header.startswith(sig) for sig in IMAGE_SIGNATURES[ext]):
        return None, "That file doesn't look like a real PNG/JPG image."

    filename = f"{secrets.token_hex(16)}{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, filename))
    return filename, None


def _looks_like_mp4_family(header):
    # ISO base media file format: a 4-byte box size, then the 4-byte box
    # type "ftyp" — the size varies, so this isn't a simple fixed prefix
    # like the image signatures, it's checked at a fixed offset instead.
    # .mp4 and .mov (QuickTime — what iPhones actually record video as)
    # are both built on this same container format, so one check covers
    # both.
    return len(header) >= 8 and header[4:8] == b"ftyp"


def _looks_like_webm(header):
    # WebM/Matroska files start with this exact EBML header.
    return header.startswith(b"\x1a\x45\xdf\xa3")


VIDEO_VALIDATORS = {
    ".mp4": _looks_like_mp4_family,
    ".mov": _looks_like_mp4_family,
    ".webm": _looks_like_webm,
}


def save_uploaded_video(file_storage):
    """Same idea as save_uploaded_image, for video. MP4, MOV (what iPhones
    save recorded video as), and WebM are accepted — everything modern
    browsers can play natively with <video>, no plugins or server-side
    transcoding required."""
    if file_storage is None or not file_storage.filename:
        return None, None

    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in VIDEO_VALIDATORS:
        return None, "Only MP4, MOV, and WebM videos are allowed."

    header = file_storage.stream.read(32)
    file_storage.stream.seek(0)
    if not VIDEO_VALIDATORS[ext](header):
        return None, "That file doesn't look like a real video."

    filename = f"{secrets.token_hex(16)}{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, filename))
    return filename, None


def delete_uploaded_file(filename):
    """Removes an uploaded image or video from disk. Safe to call with a
    filename that's already gone (or None) — just does nothing."""
    if not filename:
        return
    try:
        os.remove(os.path.join(UPLOAD_DIR, filename))
    except OSError:
        pass  # already gone, or never existed — fine either way


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            avatar TEXT NOT NULL DEFAULT '🦁',
            avatar_image_filename TEXT,
            bio TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            image_filename TEXT,
            video_filename TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
            UNIQUE(user_id, broadcast_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tags (
            name TEXT PRIMARY KEY,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            image_filename TEXT,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            UNIQUE(channel_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target_type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_pair
            ON messages (sender_id, receiver_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_channel_subs
            ON channel_subscriptions (user_id, channel_id);

        CREATE INDEX IF NOT EXISTS idx_push_subs_user
            ON push_subscriptions (user_id);

        CREATE INDEX IF NOT EXISTS idx_reports_status
            ON reports (status, created_at);
        """
    )
    db.commit()

    # Lightweight migration for DBs created before the ban feature existed.
    existing_cols = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if "banned_until" not in existing_cols:
        # NULL = not banned. The literal string "forever" = permanent ban.
        # Any other value is an ISO timestamp the ban expires at.
        db.execute("ALTER TABLE users ADD COLUMN banned_until TEXT")
        db.commit()
    if "is_verified" not in existing_cols:
        db.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0")
        db.commit()
    if "avatar_image_filename" not in existing_cols:
        db.execute("ALTER TABLE users ADD COLUMN avatar_image_filename TEXT")
        db.commit()

    # Lightweight migration for DBs created with the old tags.created_by
    # ON DELETE CASCADE — that setup silently deleted a tag (un-registering
    # it for everyone) whenever its creator's account was removed. Rebuild
    # the table so account deletion only clears attribution, never the tag.
    tags_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tags'"
    ).fetchone()
    if tags_exists:
        fk_info = db.execute("PRAGMA foreign_key_list(tags)").fetchall()
        needs_tags_fix = any(row[6] == "CASCADE" for row in fk_info)
        if needs_tags_fix:
            db.executescript(
                """
                ALTER TABLE tags RENAME TO tags_old;
                CREATE TABLE tags (
                    name TEXT PRIMARY KEY,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO tags (name, created_by, created_at)
                    SELECT name, created_by, created_at FROM tags_old;
                DROP TABLE tags_old;
                """
            )
            db.commit()

    # Lightweight migration for DBs created before broadcast image uploads.
    broadcast_cols = {row[1] for row in db.execute("PRAGMA table_info(broadcasts)")}
    if "image_filename" not in broadcast_cols:
        db.execute("ALTER TABLE broadcasts ADD COLUMN image_filename TEXT")
        db.commit()
    if "video_filename" not in broadcast_cols:
        db.execute("ALTER TABLE broadcasts ADD COLUMN video_filename TEXT")
        db.commit()
    if "channel_id" not in broadcast_cols:
        db.execute("ALTER TABLE broadcasts ADD COLUMN channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL")
        db.commit()

    channel_cols = {row[1] for row in db.execute("PRAGMA table_info(channels)")}
    if "image_filename" not in channel_cols:
        db.execute("ALTER TABLE channels ADD COLUMN image_filename TEXT")
        db.commit()

    # Lightweight migration for DBs created before DM photo/video attachments.
    message_cols = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    if "image_filename" not in message_cols:
        db.execute("ALTER TABLE messages ADD COLUMN image_filename TEXT")
        db.commit()
    if "video_filename" not in message_cols:
        db.execute("ALTER TABLE messages ADD COLUMN video_filename TEXT")
        db.commit()

    db.close()


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in first.", "error")
            return redirect(url_for("login", next=request.path))
        user = current_user()
        if user is None:
            # Session cookie points at a user that no longer exists in the
            # database (e.g. the db file was reset/replaced) — clear the
            # stale session instead of silently rendering a half-broken page.
            session.clear()
            flash("Your session expired — please log in again.", "error")
            return redirect(url_for("login", next=request.path))
        if is_user_banned(user):
            # Catches a ban that happened while this user already had an
            # active session, not just at their next login attempt.
            session.clear()
            flash(ban_message(user), "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()


def is_admin_user(user):
    # Case-sensitive on purpose — see the ADMIN_USERNAMES comment.
    return bool(user) and user["username"] in ADMIN_USERNAMES


def is_verified_user(user):
    return bool(user) and bool(user["is_verified"])


def can_create_tags(user):
    """Admins and verified accounts can originate brand-new #tags. Everyone
    else can only use tags that already exist in the tags registry."""
    return is_admin_user(user) or is_verified_user(user)


def can_create_channels(user):
    """Same rule as tags, kept as its own name in case the two ever
    diverge — admins and verified accounts can create channels."""
    return is_admin_user(user) or is_verified_user(user)


def is_user_banned(user):
    if not user or not user["banned_until"]:
        return False
    if user["banned_until"] == "forever":
        return True
    try:
        until = datetime.fromisoformat(user["banned_until"])
    except ValueError:
        return False
    return datetime.now(timezone.utc) < until


def ban_label(user):
    """Human-readable ban status for display — None if not currently banned."""
    if not is_user_banned(user):
        return None
    if user["banned_until"] == "forever":
        return "permanently"
    until = datetime.fromisoformat(user["banned_until"])
    return f"until {until.strftime('%Y-%m-%d %H:%M UTC')}"


def ban_message(user):
    label = ban_label(user)
    if label == "permanently":
        return "This account has been permanently banned."
    return f"This account is banned {label}."


@app.context_processor
def inject_user():
    user = current_user()
    admin = is_admin_user(user)
    open_report_count = 0
    if admin:
        open_report_count = get_db().execute(
            "SELECT COUNT(*) c FROM reports WHERE status = 'open'"
        ).fetchone()["c"]
    return {
        "current_user": user,
        "is_admin": admin,
        "is_verified": is_verified_user(user),
        "can_create_tags": can_create_tags(user),
        "can_create_channels": can_create_channels(user),
        "admin_usernames": ADMIN_USERNAMES,
        "admin_open_report_count": open_report_count,
        # Read server-side from a cookie (not just localStorage) so the
        # correct theme class can be baked directly into the very first
        # byte of HTML the server sends — no client-side JS needs to run
        # before first paint to avoid a flash of the wrong theme. This is
        # the actual fix for the flash; the inline <head> script in
        # base.html is now just a same-tab-toggle-without-reload nicety
        # and a fallback for the rare case cookies are blocked.
        "dark_theme": request.cookies.get("flycord_theme") == "dark",
    }


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"#(\w+)")


def extract_tags(text):
    return sorted(set(t.lower() for t in TAG_RE.findall(text)))


def existing_tag_names(names):
    """Given a list of lowercase tag names, return the subset that already
    exist in the tags registry."""
    if not names:
        return set()
    db = get_db()
    placeholders = ",".join("?" for _ in names)
    rows = db.execute(
        f"SELECT name FROM tags WHERE name IN ({placeholders})", list(names)
    ).fetchall()
    return {r["name"] for r in rows}


def register_new_tags(names, user_id):
    """Record brand-new tags in the registry, crediting the creator."""
    if not names:
        return
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.executemany(
        "INSERT OR IGNORE INTO tags (name, created_by, created_at) VALUES (?, ?, ?)",
        [(name, user_id, now) for name in names],
    )
    db.commit()


def slugify_channel_name(name):
    """Turn a channel name into a URL-safe, unique slug — lowercase,
    non-alphanumerics collapsed to single hyphens, with a numeric suffix
    added if the base slug is already taken."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    base = base or "channel"
    db = get_db()
    slug = base
    n = 2
    while db.execute("SELECT 1 FROM channels WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def subscribed_channel_ids(user_id):
    db = get_db()
    rows = db.execute(
        "SELECT channel_id FROM channel_subscriptions WHERE user_id = ?", (user_id,)
    ).fetchall()
    return [r["channel_id"] for r in rows]


def linkify(text):
    """Turn #tags into clickable links; escape everything else."""
    escaped = str(escape(text))

    def repl(m):
        tag = m.group(1)
        return f'<a class="tag-link" href="{url_for("feed")}?tag={tag}">#{tag}</a>'

    return Markup(TAG_RE.sub(repl, escaped))


def time_ago(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def avatar_html(row, size="avatar-sm"):
    """Render a user's avatar — their uploaded picture if they have one,
    otherwise a plain generic guest icon (no emoji avatars anymore).

    Safe to call from a template fed by ANY query, even one that never
    selected avatar_image_filename (most avatar-carrying queries only
    select a couple of columns, not the whole row) — it just falls back
    to the guest icon in that case.
    """
    image_filename = None
    keys = row.keys() if hasattr(row, "keys") else []
    if "avatar_image_filename" in keys:
        image_filename = row["avatar_image_filename"]

    if image_filename:
        url = url_for("static", filename="uploads/" + image_filename)
        return Markup(f'<img class="{size} avatar-img" src="{escape(url)}" alt="">')

    return Markup(f'<span class="{size} avatar-guest">{GUEST_AVATAR_SVG}</span>')


app.jinja_env.filters["linkify"] = linkify
app.jinja_env.filters["time_ago"] = time_ago
app.jinja_env.globals["avatar_html"] = avatar_html


def versioned_static(filename):
    """Same URL as url_for('static', ...) but with a ?v=<mtime> tacked on,
    so browsers (mobile Safari especially) can't keep serving a stale
    cached copy of style.css/main.js after we ship a change — the query
    string changes automatically whenever the file's contents change,
    with no manual version bump needed."""
    path = os.path.join(app.static_folder, filename)
    try:
        version = int(os.path.getmtime(path))
    except OSError:
        version = 0
    return f"{url_for('static', filename=filename)}?v={version}"


app.jinja_env.globals["versioned_static"] = versioned_static


def top_tags(limit=5):
    db = get_db()
    rows = db.execute("SELECT content FROM broadcasts").fetchall()
    counts = {}
    for row in rows:
        for tag in extract_tags(row["content"]):
            counts[tag] = counts.get(tag, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:limit]


def build_feed_query(tag=None, mine_user_id=None, min_id=None, order="DESC", channel_ids=None,
                      exclude_channel_posts=False):
    query = """
        SELECT b.*, u.username, u.avatar, u.avatar_image_filename, u.is_verified AS author_is_verified,
               ch.name AS channel_name, ch.slug AS channel_slug, ch.image_filename AS channel_image_filename,
               (SELECT COUNT(*) FROM likes l WHERE l.broadcast_id = b.id) AS like_count,
               (SELECT COUNT(*) FROM comments c WHERE c.broadcast_id = b.id) AS comment_count
        FROM broadcasts b
        JOIN users u ON u.id = b.user_id
        LEFT JOIN channels ch ON ch.id = b.channel_id
    """
    conditions = []
    params = []
    if mine_user_id is not None:
        conditions.append("b.user_id = ?")
        params.append(mine_user_id)
    if tag:
        conditions.append("LOWER(b.content) LIKE ?")
        params.append(f"%#{tag.lower()}%")
    if min_id is not None:
        conditions.append("b.id > ?")
        params.append(min_id)
    if channel_ids is not None:
        if channel_ids:
            placeholders = ",".join("?" for _ in channel_ids)
            conditions.append(f"b.channel_id IN ({placeholders})")
            params.extend(channel_ids)
        else:
            # Filtering to "subscribed channels" but subscribed to none —
            # an always-false condition gives a clean empty result set
            # instead of a separate code path.
            conditions.append("0")
    if exclude_channel_posts:
        # Channel broadcasts only live in the "My Channels" tab — Global
        # All is personal broadcasts only.
        conditions.append("b.channel_id IS NULL")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" ORDER BY b.created_at {order}"
    return query, params


def liked_ids_for(user_id, broadcast_ids):
    if not user_id or not broadcast_ids:
        return set()
    db = get_db()
    placeholders = ",".join("?" for _ in broadcast_ids)
    rows = db.execute(
        f"SELECT broadcast_id FROM likes WHERE user_id = ? AND broadcast_id IN ({placeholders})",
        [user_id] + list(broadcast_ids),
    ).fetchall()
    return {r["broadcast_id"] for r in rows}


# --------------------------------------------------------------------------
# Routes — feed
# --------------------------------------------------------------------------

@app.route("/")
@login_required
def feed():
    tag = request.args.get("tag", "").strip() or None
    view = request.args.get("view", "all")
    db = get_db()
    uid = session["user_id"]

    mine_user_id = uid if view == "mine" else None
    channel_ids = subscribed_channel_ids(uid) if view == "channels" else None
    query, params = build_feed_query(
        tag=tag, mine_user_id=mine_user_id, channel_ids=channel_ids,
        exclude_channel_posts=(view == "all"),
    )
    posts = db.execute(query, params).fetchall()

    liked = liked_ids_for(uid, [p["id"] for p in posts])

    my_channels = db.execute(
        "SELECT id, name, slug FROM channels WHERE owner_id = ? ORDER BY name ASC", (uid,)
    ).fetchall()

    return render_template(
        "feed.html",
        posts=posts,
        liked=liked,
        tag=tag,
        view=view,
        trends=top_tags(),
        max_len=BROADCAST_MAX_LEN,
        my_channels=my_channels,
    )


@app.route("/broadcast", methods=["POST"])
@login_required
def broadcast():
    content = request.form.get("content", "").strip()

    image_filename, image_error = save_uploaded_image(request.files.get("image"))
    if image_error:
        flash(image_error, "error")
        return redirect(request.referrer or url_for("feed"))

    video_filename, video_error = save_uploaded_video(request.files.get("video"))
    if video_error:
        delete_uploaded_file(image_filename)
        flash(video_error, "error")
        return redirect(request.referrer or url_for("feed"))

    if not content and not image_filename and not video_filename:
        flash("Your broadcast can't be empty.", "error")
        return redirect(request.referrer or url_for("feed"))

    if len(content) > BROADCAST_MAX_LEN:
        flash(f"Broadcast too long (max {BROADCAST_MAX_LEN} characters).", "error")
        delete_uploaded_file(image_filename)
        delete_uploaded_file(video_filename)
        return redirect(request.referrer or url_for("feed"))

    user = current_user()
    tags_used = extract_tags(content)
    new_tags = []
    if tags_used:
        already_known = existing_tag_names(tags_used)
        new_tags = [t for t in tags_used if t not in already_known]
        if new_tags and not can_create_tags(user):
            tag_list = ", ".join(f"#{t}" for t in new_tags)
            flash(
                f"Only admins and verified accounts can create new tags. "
                f"{tag_list} doesn't exist yet — remove it or use an existing tag.",
                "error",
            )
            delete_uploaded_file(image_filename)
            delete_uploaded_file(video_filename)
            return redirect(request.referrer or url_for("feed"))

    db = get_db()

    # Optional: post into one of the channels the user owns, instead of
    # just their personal feed. Silently ignored (posts to personal feed
    # as normal) if they don't actually own the channel they picked —
    # this only matters if someone tampers with the form.
    channel_id = None
    raw_channel_id = request.form.get("channel_id", "").strip()
    if raw_channel_id:
        channel_row = db.execute(
            "SELECT id FROM channels WHERE id = ? AND owner_id = ?", (raw_channel_id, user["id"])
        ).fetchone()
        if channel_row:
            channel_id = channel_row["id"]

    db.execute(
        "INSERT INTO broadcasts (user_id, content, image_filename, video_filename, channel_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session["user_id"], content, image_filename, video_filename, channel_id, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    if new_tags:
        register_new_tags(new_tags, user["id"])
    return redirect(request.referrer or url_for("feed"))


@app.route("/post/<int:post_id>")
@login_required
def post_detail(post_id):
    db = get_db()
    post = db.execute(
        """
        SELECT b.*, u.username, u.avatar, u.avatar_image_filename, u.is_verified AS author_is_verified,
               ch.name AS channel_name, ch.slug AS channel_slug, ch.image_filename AS channel_image_filename,
               (SELECT COUNT(*) FROM likes l WHERE l.broadcast_id = b.id) AS like_count,
               (SELECT COUNT(*) FROM comments c WHERE c.broadcast_id = b.id) AS comment_count
        FROM broadcasts b
        JOIN users u ON u.id = b.user_id
        LEFT JOIN channels ch ON ch.id = b.channel_id
        WHERE b.id = ?
        """,
        (post_id,),
    ).fetchone()
    if post is None:
        abort(404)
    comments = db.execute(
        """
        SELECT c.*, u.username, u.avatar, u.avatar_image_filename, u.is_verified AS author_is_verified
        FROM comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.broadcast_id = ?
        ORDER BY c.created_at ASC
        """,
        (post_id,),
    ).fetchall()
    liked = liked_ids_for(session["user_id"], [post_id])
    return render_template("post.html", post=post, comments=comments, liked=liked)


@app.route("/post/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    db = get_db()
    post = db.execute(
        "SELECT user_id, image_filename, video_filename FROM broadcasts WHERE id = ?", (post_id,)
    ).fetchone()
    if post is None:
        if wants_json():
            return jsonify({"error": "Not found."}), 404
        abort(404)

    user = current_user()
    is_owner = post["user_id"] == user["id"]
    if not (is_owner or is_admin_user(user)):
        if wants_json():
            return jsonify({"error": "Not allowed."}), 403
        abort(403)

    # ON DELETE CASCADE (foreign_keys pragma is on) cleans up likes/comments.
    db.execute("DELETE FROM broadcasts WHERE id = ?", (post_id,))
    db.commit()
    delete_uploaded_file(post["image_filename"])
    delete_uploaded_file(post["video_filename"])

    if wants_json():
        return jsonify({"deleted": True, "id": post_id})

    flash("Broadcast deleted.", "success")
    return redirect(request.referrer or url_for("feed"))


@app.route("/post/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM likes WHERE user_id = ? AND broadcast_id = ?",
        (session["user_id"], post_id),
    ).fetchone()
    if existing:
        db.execute("DELETE FROM likes WHERE id = ?", (existing["id"],))
        now_liked = False
    else:
        db.execute(
            "INSERT INTO likes (user_id, broadcast_id) VALUES (?, ?)",
            (session["user_id"], post_id),
        )
        now_liked = True
    db.commit()

    if wants_json():
        count = db.execute(
            "SELECT COUNT(*) c FROM likes WHERE broadcast_id = ?", (post_id,)
        ).fetchone()["c"]
        return jsonify({"liked": now_liked, "like_count": count})

    return redirect(request.referrer or url_for("feed"))


@app.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def comment_post(post_id):
    content = request.form.get("content", "").strip()
    if content:
        db = get_db()
        db.execute(
            "INSERT INTO comments (broadcast_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (post_id, session["user_id"], content, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()

        if wants_json():
            user = current_user()
            return jsonify({
                "id": post_id,
                "username": user["username"],
                "avatar": user["avatar"],
                "content_html": str(linkify(content)),
                "time_label": "just now",
            })

    return redirect(url_for("post_detail", post_id=post_id))


# --------------------------------------------------------------------------
# Routes — explore
# --------------------------------------------------------------------------

@app.route("/explore")
@login_required
def explore():
    db = get_db()
    rows = db.execute(
        """
        SELECT u.*, (SELECT COUNT(*) FROM broadcasts b WHERE b.user_id = u.id) AS post_count
        FROM users u ORDER BY u.created_at DESC
        """
    ).fetchall()
    users = [dict(r, is_banned=is_user_banned(r)) for r in rows]
    return render_template("explore.html", users=users, trends=top_tags(limit=10))


# --------------------------------------------------------------------------
# Routes — channels
#
# Verified accounts and admins can create channels; anyone can subscribe.
# A subscriber's Global Feed gets a "My Channels" tab showing a combined
# feed of everything posted into channels they follow. Channel posts do
# NOT show up in the Global All tab — that's personal broadcasts only.
# Channel creators also stay anonymous: the channels list never reveals
# who owns a channel, only its name/picture/description.
# --------------------------------------------------------------------------

CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{1,38}[A-Za-z0-9]$")


@app.route("/channels")
@login_required
def channels_list():
    db = get_db()
    uid = session["user_id"]
    rows = db.execute(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM channel_subscriptions s WHERE s.channel_id = c.id) AS subscriber_count,
               (SELECT COUNT(*) FROM broadcasts b WHERE b.channel_id = c.id) AS post_count,
               EXISTS(SELECT 1 FROM channel_subscriptions s WHERE s.channel_id = c.id AND s.user_id = ?) AS subscribed
        FROM channels c
        ORDER BY c.created_at DESC
        """,
        (uid,),
    ).fetchall()
    return render_template("channels.html", channels=rows)


@app.route("/channels/create", methods=["POST"])
@login_required
def create_channel():
    user = current_user()
    if not can_create_channels(user):
        flash("Only verified accounts and admins can create channels.", "error")
        return redirect(url_for("channels_list"))

    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()[:200]

    if not CHANNEL_NAME_RE.match(name):
        flash("Channel names need to be 3-40 characters: letters, numbers, spaces, underscore, or hyphen.", "error")
        return redirect(url_for("channels_list"))

    image_filename, image_error = save_uploaded_image(request.files.get("image"))
    if image_error:
        flash(image_error, "error")
        return redirect(url_for("channels_list"))

    db = get_db()
    slug = slugify_channel_name(name)
    db.execute(
        "INSERT INTO channels (name, slug, description, image_filename, owner_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, slug, description, image_filename, user["id"], datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    flash(f'Channel "{name}" created.', "success")
    return redirect(url_for("channels_list"))


@app.route("/channels/<slug>/subscribe", methods=["POST"])
@login_required
def subscribe_channel(slug):
    db = get_db()
    channel = db.execute("SELECT * FROM channels WHERE slug = ?", (slug,)).fetchone()
    if channel is None:
        abort(404)
    db.execute(
        "INSERT OR IGNORE INTO channel_subscriptions (channel_id, user_id, created_at) VALUES (?, ?, ?)",
        (channel["id"], session["user_id"], datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return redirect(request.referrer or url_for("channels_list"))


@app.route("/channels/<slug>/unsubscribe", methods=["POST"])
@login_required
def unsubscribe_channel(slug):
    db = get_db()
    channel = db.execute("SELECT * FROM channels WHERE slug = ?", (slug,)).fetchone()
    if channel is None:
        abort(404)
    db.execute(
        "DELETE FROM channel_subscriptions WHERE channel_id = ? AND user_id = ?",
        (channel["id"], session["user_id"]),
    )
    db.commit()
    return redirect(request.referrer or url_for("channels_list"))


@app.route("/channels/<slug>/delete", methods=["POST"])
@login_required
def delete_channel(slug):
    user = current_user()
    db = get_db()
    channel = db.execute("SELECT * FROM channels WHERE slug = ?", (slug,)).fetchone()
    if channel is None:
        abort(404)
    if not (is_admin_user(user) or channel["owner_id"] == user["id"]):
        abort(403)

    # Broadcasts already posted into this channel aren't deleted — they
    # just fall back to being regular personal broadcasts (channel_id goes
    # NULL via the ON DELETE SET NULL foreign key).
    db.execute("DELETE FROM channels WHERE id = ?", (channel["id"],))
    db.commit()
    delete_uploaded_file(channel["image_filename"])
    flash(f'Channel "{channel["name"]}" deleted.', "success")
    return redirect(url_for("channels_list"))


# --------------------------------------------------------------------------
# Routes — search
# --------------------------------------------------------------------------

@app.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    users, posts = [], []

    if q:
        db = get_db()
        like_q = f"%{q}%"

        users = db.execute(
            """
            SELECT u.*, (SELECT COUNT(*) FROM broadcasts b WHERE b.user_id = u.id) AS post_count
            FROM users u
            WHERE u.username LIKE ? OR u.bio LIKE ?
            ORDER BY u.username ASC
            LIMIT 20
            """,
            (like_q, like_q),
        ).fetchall()

        posts = db.execute(
            """
            SELECT b.*, u.username, u.avatar, u.avatar_image_filename, u.is_verified AS author_is_verified,
                   ch.name AS channel_name, ch.slug AS channel_slug, ch.image_filename AS channel_image_filename,
                   (SELECT COUNT(*) FROM likes l WHERE l.broadcast_id = b.id) AS like_count,
                   (SELECT COUNT(*) FROM comments c WHERE c.broadcast_id = b.id) AS comment_count
            FROM broadcasts b
            JOIN users u ON u.id = b.user_id
            LEFT JOIN channels ch ON ch.id = b.channel_id
            WHERE b.content LIKE ?
            ORDER BY b.created_at DESC
            LIMIT 30
            """,
            (like_q,),
        ).fetchall()

    liked = liked_ids_for(session["user_id"], [p["id"] for p in posts]) if posts else set()
    return render_template("search.html", q=q, users=users, posts=posts, liked=liked)


# --------------------------------------------------------------------------
# Routes — direct messages
# --------------------------------------------------------------------------

@app.route("/messages")
@login_required
def messages_inbox():
    db = get_db()
    uid = session["user_id"]
    conversations = db.execute(
        """
        SELECT
            u.id AS partner_id,
            u.username AS partner_username,
            u.avatar,
            u.avatar_image_filename,
            m.content AS last_content,
            m.created_at AS last_created_at,
            m.sender_id AS last_sender_id,
            (
                SELECT COUNT(*) FROM messages um
                WHERE um.sender_id = u.id AND um.receiver_id = ? AND um.is_read = 0
            ) AS unread_count
        FROM users u
        JOIN messages m ON m.id = (
            SELECT m2.id FROM messages m2
            WHERE (m2.sender_id = u.id AND m2.receiver_id = ?)
               OR (m2.sender_id = ? AND m2.receiver_id = u.id)
            ORDER BY m2.created_at DESC LIMIT 1
        )
        WHERE u.id != ?
        ORDER BY m.created_at DESC
        """,
        (uid, uid, uid, uid),
    ).fetchall()
    return render_template("messages_inbox.html", conversations=conversations)


@app.route("/messages/<username>")
@login_required
def message_thread(username):
    db = get_db()
    uid = session["user_id"]
    partner = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if partner is None:
        abort(404)
    if partner["id"] == uid:
        abort(404)

    msgs = db.execute(
        """
        SELECT m.*, u.username, u.avatar FROM messages m
        JOIN users u ON u.id = m.sender_id
        WHERE (m.sender_id = ? AND m.receiver_id = ?)
           OR (m.sender_id = ? AND m.receiver_id = ?)
        ORDER BY m.created_at ASC
        """,
        (uid, partner["id"], partner["id"], uid),
    ).fetchall()

    db.execute(
        "UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ? AND is_read = 0",
        (partner["id"], uid),
    )
    db.commit()

    last_id = msgs[-1]["id"] if msgs else 0
    return render_template(
        "message_thread.html", partner=partner, messages=msgs,
        last_id=last_id, max_len=MESSAGE_MAX_LEN,
    )


@app.route("/messages/<username>/send", methods=["POST"])
@login_required
def send_message(username):
    db = get_db()
    uid = session["user_id"]
    partner = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if partner is None or partner["id"] == uid:
        abort(404)

    content = request.form.get("content", "").strip()

    image_filename, image_error = save_uploaded_image(request.files.get("image"))
    if image_error:
        if wants_json():
            return jsonify({"error": image_error}), 400
        flash(image_error, "error")
        return redirect(url_for("message_thread", username=username))

    video_filename, video_error = save_uploaded_video(request.files.get("video"))
    if video_error:
        delete_uploaded_file(image_filename)
        if wants_json():
            return jsonify({"error": video_error}), 400
        flash(video_error, "error")
        return redirect(url_for("message_thread", username=username))

    if not content and not image_filename and not video_filename:
        delete_uploaded_file(image_filename)
        delete_uploaded_file(video_filename)
        if wants_json():
            return jsonify({"error": "Message can't be empty."}), 400
        flash("Message can't be empty.", "error")
        return redirect(url_for("message_thread", username=username))

    if len(content) > MESSAGE_MAX_LEN:
        delete_uploaded_file(image_filename)
        delete_uploaded_file(video_filename)
        if wants_json():
            return jsonify({"error": "Message too long."}), 400
        flash(f"Message too long (max {MESSAGE_MAX_LEN} characters).", "error")
        return redirect(url_for("message_thread", username=username))

    now = datetime.now(timezone.utc).isoformat()
    cur = db.execute(
        "INSERT INTO messages (sender_id, receiver_id, content, image_filename, video_filename, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uid, partner["id"], content, image_filename, video_filename, now),
    )
    db.commit()

    me = current_user()
    push_preview = content[:120] if content else ("📷 Sent a photo" if image_filename else "🎥 Sent a video")
    send_push_to_user(
        partner["id"],
        title=f"{me['username']} sent you a message",
        body=push_preview,
        url=f"/messages/{me['username']}",
    )

    if wants_json():
        return jsonify({
            "id": cur.lastrowid,
            "sender_id": uid,
            "username": me["username"],
            "avatar": me["avatar"],
            "content": content,
            "image_url": url_for("static", filename="uploads/" + image_filename) if image_filename else None,
            "video_url": url_for("static", filename="uploads/" + video_filename) if video_filename else None,
            "created_at": now,
        })

    return redirect(url_for("message_thread", username=username))


# --------------------------------------------------------------------------
# Routes — web push notifications
# --------------------------------------------------------------------------

@app.route("/push/vapid-public-key")
@login_required
def push_vapid_public_key():
    return jsonify({"key": vapid_public_key_b64()})


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "Incomplete subscription."}), 400

    db = get_db()
    db.execute(
        """
        INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            user_id = excluded.user_id, p256dh = excluded.p256dh,
            auth = excluded.auth, created_at = excluded.created_at
        """,
        (session["user_id"], endpoint, p256dh, auth, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    if endpoint:
        db = get_db()
        db.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
            (endpoint, session["user_id"]),
        )
        db.commit()
    return jsonify({"ok": True})


@app.route("/sw.js")
def service_worker():
    # Served from the site root (not /static/sw.js) on purpose — a service
    # worker's scope is limited to the directory it's served from, and it
    # needs to control every page on the site, not just /static/.
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


# --------------------------------------------------------------------------
# Routes — profile
# --------------------------------------------------------------------------

@app.route("/profile")
@login_required
def profile_redirect():
    user = current_user()
    return redirect(url_for("user_profile", username=user["username"]))


@app.route("/u/<username>")
@login_required
def user_profile(username):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if user is None:
        abort(404)
    query, params = build_feed_query(mine_user_id=user["id"])
    posts = db.execute(query, params).fetchall()
    liked = liked_ids_for(session["user_id"], [p["id"] for p in posts])
    return render_template(
        "profile.html", profile_user=user, posts=posts, liked=liked,
        target_is_banned=is_user_banned(user),
        target_ban_label=ban_label(user),
    )


# --------------------------------------------------------------------------
# Routes — admin (ban / unban)
# --------------------------------------------------------------------------

@app.route("/admin/users/<username>/ban", methods=["POST"])
@login_required
def ban_user(username):
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    target = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        abort(404)
    if is_admin_user(target):
        flash("Admin accounts can't be banned.", "error")
        return redirect(request.referrer or url_for("user_profile", username=username))

    mode = request.form.get("mode", "temp")
    if mode == "forever":
        banned_until = "forever"
        label = "permanently"
    else:
        try:
            amount = int(request.form.get("amount", 1))
        except ValueError:
            amount = 1
        amount = max(1, amount)
        unit = request.form.get("unit", "hours")
        unit_kwargs = {
            "minutes": {"minutes": amount},
            "hours": {"hours": amount},
            "days": {"days": amount},
        }.get(unit, {"hours": amount})
        until_dt = datetime.now(timezone.utc) + timedelta(**unit_kwargs)
        banned_until = until_dt.isoformat()
        label = f"for {amount} {unit}"

    db.execute("UPDATE users SET banned_until = ? WHERE id = ?", (banned_until, target["id"]))
    db.commit()
    flash(f"{target['username']} has been banned {label}.", "success")
    return redirect(request.referrer or url_for("user_profile", username=username))


@app.route("/admin/users/<username>/unban", methods=["POST"])
@login_required
def unban_user(username):
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    target = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        abort(404)

    db.execute("UPDATE users SET banned_until = NULL WHERE id = ?", (target["id"],))
    db.commit()
    flash(f"{target['username']} has been unbanned.", "success")
    return redirect(request.referrer or url_for("user_profile", username=username))


@app.route("/admin/users/<username>/delete", methods=["POST"])
@login_required
def delete_user(username):
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    target = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        abort(404)
    if is_admin_user(target):
        flash("Admin accounts can't be deleted.", "error")
        return redirect(request.referrer or url_for("user_profile", username=username))
    if not is_user_banned(target):
        # Safety rail: an admin has to ban an account before they can
        # permanently delete it — makes accidental deletion much harder.
        flash("Ban this account first, then you can delete it.", "error")
        return redirect(request.referrer or url_for("user_profile", username=username))

    # Clean up any uploaded image/video files before the DB rows (and their
    # cascade-deleted broadcasts/messages) disappear — otherwise they'd be
    # orphaned on disk forever.
    media_rows = db.execute(
        """
        SELECT image_filename, video_filename FROM broadcasts
        WHERE user_id = ? AND (image_filename IS NOT NULL OR video_filename IS NOT NULL)
        """,
        (target["id"],),
    ).fetchall()
    message_media_rows = db.execute(
        """
        SELECT image_filename, video_filename FROM messages
        WHERE sender_id = ? AND (image_filename IS NOT NULL OR video_filename IS NOT NULL)
        """,
        (target["id"],),
    ).fetchall()

    deleted_username = target["username"]
    db.execute("DELETE FROM users WHERE id = ?", (target["id"],))
    db.commit()

    for row in media_rows:
        delete_uploaded_file(row["image_filename"])
        delete_uploaded_file(row["video_filename"])
    for row in message_media_rows:
        delete_uploaded_file(row["image_filename"])
        delete_uploaded_file(row["video_filename"])

    flash(f"{deleted_username}'s account has been permanently deleted.", "success")
    return redirect(url_for("explore"))


@app.route("/admin/users/<username>/verify", methods=["POST"])
@login_required
def verify_user(username):
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    target = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        abort(404)
    if is_admin_user(target):
        flash("Admins already have full posting privileges.", "error")
        return redirect(request.referrer or url_for("user_profile", username=username))

    db.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (target["id"],))
    db.commit()
    flash(f"{target['username']} is now verified and can create new #tags.", "success")
    return redirect(request.referrer or url_for("user_profile", username=username))


@app.route("/admin/users/<username>/unverify", methods=["POST"])
@login_required
def unverify_user(username):
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    target = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        abort(404)

    db.execute("UPDATE users SET is_verified = 0 WHERE id = ?", (target["id"],))
    db.commit()
    flash(f"{target['username']}'s verified badge has been removed.", "success")
    return redirect(request.referrer or url_for("user_profile", username=username))


@app.route("/admin/users/<username>/reset-password", methods=["POST"])
@login_required
def reset_password(username):
    """Admin can force a password reset without ever seeing (or needing to
    see) the account's actual password — passwords are one-way hashed and
    genuinely can't be recovered, by design. This generates a fresh random
    temporary password, shown once to the admin to relay to the user, who
    should change it via Settings right after logging in with it."""
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    target = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        abort(404)
    if is_admin_user(target):
        flash("Admin accounts can't be reset this way.", "error")
        return redirect(request.referrer or url_for("user_profile", username=username))

    temp_password = secrets.token_urlsafe(9)  # ~12 readable chars
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(temp_password), target["id"]),
    )
    db.commit()
    flash(
        f"Temporary password for {target['username']}: {temp_password} — "
        f"give this to them now, it won't be shown again. They should change "
        f"it in Settings as soon as they log in.",
        "success",
    )
    return redirect(request.referrer or url_for("user_profile", username=username))


# --------------------------------------------------------------------------
# Routes — reports
#
# Any logged-in user can report a broadcast or another account. Reports go
# straight to the admin's Reports page — nothing is hidden or auto-actioned,
# an admin always reviews it and decides what (if anything) to do.
# --------------------------------------------------------------------------

REPORT_REASON_MAX_LEN = 300


@app.route("/report/broadcast/<int:post_id>", methods=["POST"])
@login_required
def report_broadcast(post_id):
    db = get_db()
    post = db.execute("SELECT id FROM broadcasts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        abort(404)

    reason = request.form.get("reason", "").strip()[:REPORT_REASON_MAX_LEN]
    if not reason:
        flash("Please say a little about why you're reporting this.", "error")
        return redirect(request.referrer or url_for("feed"))

    db.execute(
        "INSERT INTO reports (reporter_id, target_type, target_id, reason, created_at) VALUES (?, 'broadcast', ?, ?, ?)",
        (session["user_id"], post_id, reason, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    flash("Report submitted — thanks for helping keep FlyCord safe.", "success")
    return redirect(request.referrer or url_for("feed"))


@app.route("/report/user/<username>", methods=["POST"])
@login_required
def report_user(username):
    db = get_db()
    target = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        abort(404)
    if target["id"] == session["user_id"]:
        flash("You can't report yourself.", "error")
        return redirect(request.referrer or url_for("user_profile", username=username))

    reason = request.form.get("reason", "").strip()[:REPORT_REASON_MAX_LEN]
    if not reason:
        flash("Please say a little about why you're reporting this account.", "error")
        return redirect(request.referrer or url_for("user_profile", username=username))

    db.execute(
        "INSERT INTO reports (reporter_id, target_type, target_id, reason, created_at) VALUES (?, 'user', ?, ?, ?)",
        (session["user_id"], target["id"], reason, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    flash("Report submitted — thanks for helping keep FlyCord safe.", "success")
    return redirect(request.referrer or url_for("user_profile", username=username))


@app.route("/admin/reports")
@login_required
def admin_reports():
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    status_filter = request.args.get("status", "open")
    if status_filter not in ("open", "resolved", "all"):
        status_filter = "open"

    rows = db.execute(
        """
        SELECT r.*, u.username AS reporter_username, u.avatar AS reporter_avatar,
               u.avatar_image_filename AS reporter_avatar_image_filename
        FROM reports r
        JOIN users u ON u.id = r.reporter_id
        ORDER BY r.status = 'open' DESC, r.created_at DESC
        """
    ).fetchall()

    reports = []
    for r in rows:
        if status_filter != "all" and r["status"] != status_filter:
            continue
        entry = dict(r)
        if r["target_type"] == "broadcast":
            target = db.execute(
                """
                SELECT b.id, b.content, u.username, u.avatar, u.avatar_image_filename
                FROM broadcasts b JOIN users u ON u.id = b.user_id
                WHERE b.id = ?
                """,
                (r["target_id"],),
            ).fetchone()
            entry["target_exists"] = target is not None
            entry["target_summary"] = (
                f"broadcast by {target['username']}: “{target['content'][:80]}”" if target
                else "broadcast (already deleted)"
            )
            entry["target_username"] = target["username"] if target else None
        else:
            target = db.execute("SELECT id, username FROM users WHERE id = ?", (r["target_id"],)).fetchone()
            entry["target_exists"] = target is not None
            entry["target_summary"] = f"account: {target['username']}" if target else "account (already deleted)"
            entry["target_username"] = target["username"] if target else None
        reports.append(entry)

    open_count = sum(1 for r in rows if r["status"] == "open")
    return render_template("admin_reports.html", reports=reports, status_filter=status_filter, open_count=open_count)


@app.route("/admin/reports/<int:report_id>/resolve", methods=["POST"])
@login_required
def resolve_report(report_id):
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    report = db.execute("SELECT id FROM reports WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        abort(404)

    db.execute("UPDATE reports SET status = 'resolved' WHERE id = ?", (report_id,))
    db.commit()
    flash("Report marked resolved.", "success")
    return redirect(request.referrer or url_for("admin_reports"))


@app.route("/admin/reports/<int:report_id>/reopen", methods=["POST"])
@login_required
def reopen_report(report_id):
    me = current_user()
    if not is_admin_user(me):
        abort(403)

    db = get_db()
    report = db.execute("SELECT id FROM reports WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        abort(404)

    db.execute("UPDATE reports SET status = 'open' WHERE id = ?", (report_id,))
    db.commit()
    flash("Report reopened.", "success")
    return redirect(request.referrer or url_for("admin_reports"))


# --------------------------------------------------------------------------
# Routes — settings
# --------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "profile":
            bio = request.form.get("bio", "").strip()[:160]

            new_image_filename = user["avatar_image_filename"]  # keep existing by default

            if request.form.get("remove_picture"):
                delete_uploaded_file(new_image_filename)
                new_image_filename = None
            else:
                uploaded_filename, image_error = save_uploaded_image(request.files.get("picture"))
                if image_error:
                    flash(image_error, "error")
                    return redirect(url_for("settings"))
                if uploaded_filename:
                    delete_uploaded_file(user["avatar_image_filename"])  # replace, don't orphan the old one
                    new_image_filename = uploaded_filename

            db.execute(
                "UPDATE users SET avatar_image_filename = ?, bio = ? WHERE id = ?",
                (new_image_filename, bio, user["id"]),
            )
            db.commit()
            flash("Profile updated.", "success")

        elif action == "password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            if not check_password_hash(user["password_hash"], current_pw):
                flash("Current password is incorrect.", "error")
            elif len(new_pw) < MIN_PASSWORD_LEN:
                flash(f"New password must be at least {MIN_PASSWORD_LEN} characters.", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_pw), user["id"]),
                )
                db.commit()
                flash("Password changed.", "success")

        return redirect(url_for("settings"))

    return render_template("settings.html")


# --------------------------------------------------------------------------
# Routes — auth
# --------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("feed"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not re.match(r"^[A-Za-z0-9_]{3,20}$", username or ""):
            flash("Username must be 3-20 characters: letters, numbers, underscore.", "error")
        elif len(password) < MIN_PASSWORD_LEN:
            flash(f"Password must be at least {MIN_PASSWORD_LEN} characters.", "error")
        elif password != confirm:
            flash("Passwords don't match.", "error")
        else:
            db = get_db()
            # Case-insensitive check: this blocks registering "matan" or
            # "MATAN" as a separate account once "Matan" exists (or vice
            # versa) — usernames only ever differing by case would let
            # someone create a confusing lookalike of another account.
            existing = db.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            if existing:
                flash("That username is already taken.", "error")
            else:
                db.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
                user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                session["user_id"] = user["id"]
                flash(f"Welcome to FlyCord, {username}!", "success")
                return redirect(url_for("feed"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("feed"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            if is_user_banned(user):
                flash(ban_message(user), "error")
                return render_template("login.html")
            session["user_id"] = user["id"]
            flash(f"Welcome back, {username}!", "success")
            nxt = request.args.get("next") or url_for("feed")
            return redirect(nxt)
        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Small JSON API used by the Node Status widget for a "live" ping feel
# --------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    return jsonify({
        "status": "Active",
        "ping": random.randint(4, 45),
        "protocol": "v4.2-Global",
        "users": get_db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
    })


# --------------------------------------------------------------------------
# Live-update APIs (polled from the browser so pages refresh without a
# manual reload — no websockets/extra dependencies required)
# --------------------------------------------------------------------------

@app.route("/api/feed/updates")
@login_required
def api_feed_updates():
    """Return broadcasts newer than since_id, matching the current filters."""
    since_id = request.args.get("since_id", type=int, default=0)
    tag = request.args.get("tag", "").strip() or None
    view = request.args.get("view", "all")
    db = get_db()
    uid = session["user_id"]

    mine_user_id = uid if view == "mine" else None
    channel_ids = subscribed_channel_ids(uid) if view == "channels" else None
    query, params = build_feed_query(
        tag=tag, mine_user_id=mine_user_id, min_id=since_id, order="ASC", channel_ids=channel_ids,
        exclude_channel_posts=(view == "all"),
    )

    rows = db.execute(query, params).fetchall()
    liked = liked_ids_for(session["user_id"], [r["id"] for r in rows])
    me = current_user()
    admin = is_admin_user(me)

    posts = [{
        "id": r["id"],
        "username": r["username"],
        "avatar": r["avatar"],
        "avatar_url": url_for("static", filename="uploads/" + r["avatar_image_filename"]) if r["avatar_image_filename"] else None,
        "content_html": str(linkify(r["content"])) if r["content"] else "",
        "image_url": url_for("static", filename="uploads/" + r["image_filename"]) if r["image_filename"] else None,
        "video_url": url_for("static", filename="uploads/" + r["video_filename"]) if r["video_filename"] else None,
        "channel_name": r["channel_name"],
        "channel_slug": r["channel_slug"],
        "channel_image_url": url_for("static", filename="uploads/" + r["channel_image_filename"]) if r["channel_image_filename"] else None,
        "like_count": r["like_count"],
        "comment_count": r["comment_count"],
        "liked": r["id"] in liked,
        "is_mine": r["username"] == me["username"],
        "can_delete": admin or r["username"] == me["username"],
        "is_admin_author": r["username"] in ADMIN_USERNAMES,
        "is_verified_author": bool(r["author_is_verified"]),
    } for r in rows]

    return jsonify({"posts": posts})


@app.route("/api/feed/counts")
@login_required
def api_feed_counts():
    """Return current like/comment counts for a comma-separated list of post ids."""
    ids_param = request.args.get("ids", "")
    try:
        ids = [int(x) for x in ids_param.split(",") if x.strip()]
    except ValueError:
        ids = []
    if not ids:
        return jsonify({"counts": {}})

    db = get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""
        SELECT b.id,
               (SELECT COUNT(*) FROM likes l WHERE l.broadcast_id = b.id) AS like_count,
               (SELECT COUNT(*) FROM comments c WHERE c.broadcast_id = b.id) AS comment_count
        FROM broadcasts b WHERE b.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    counts = {r["id"]: {"like_count": r["like_count"], "comment_count": r["comment_count"]} for r in rows}
    return jsonify({"counts": counts})


@app.route("/api/post/<int:post_id>/comments/updates")
@login_required
def api_comment_updates(post_id):
    since_id = request.args.get("since_id", type=int, default=0)
    db = get_db()
    rows = db.execute(
        """
        SELECT c.*, u.username, u.avatar, u.avatar_image_filename, u.is_verified AS author_is_verified
        FROM comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.broadcast_id = ? AND c.id > ?
        ORDER BY c.created_at ASC
        """,
        (post_id, since_id),
    ).fetchall()
    comments = [{
        "id": r["id"],
        "username": r["username"],
        "avatar": r["avatar"],
        "avatar_url": url_for("static", filename="uploads/" + r["avatar_image_filename"]) if r["avatar_image_filename"] else None,
        "content_html": str(linkify(r["content"])),
        "time_label": time_ago(r["created_at"]),
        "is_admin_author": r["username"] in ADMIN_USERNAMES,
        "is_verified_author": bool(r["author_is_verified"]),
    } for r in rows]
    return jsonify({"comments": comments})


@app.route("/api/messages/<username>/updates")
@login_required
def api_message_updates(username):
    db = get_db()
    uid = session["user_id"]
    partner = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if partner is None:
        abort(404)
    since_id = request.args.get("since_id", type=int, default=0)

    rows = db.execute(
        """
        SELECT m.*, u.username, u.avatar FROM messages m
        JOIN users u ON u.id = m.sender_id
        WHERE ((m.sender_id = ? AND m.receiver_id = ?)
            OR (m.sender_id = ? AND m.receiver_id = ?))
          AND m.id > ?
        ORDER BY m.created_at ASC
        """,
        (uid, partner["id"], partner["id"], uid, since_id),
    ).fetchall()

    # mark any newly-fetched incoming messages as read, since the thread is open
    db.execute(
        "UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ? AND is_read = 0 AND id > ?",
        (partner["id"], uid, since_id),
    )
    db.commit()

    messages = [{
        "id": r["id"],
        "sender_id": r["sender_id"],
        "username": r["username"],
        "avatar": r["avatar"],
        "content": r["content"],
        "image_url": url_for("static", filename="uploads/" + r["image_filename"]) if r["image_filename"] else None,
        "video_url": url_for("static", filename="uploads/" + r["video_filename"]) if r["video_filename"] else None,
        "created_at": r["created_at"],
        "is_mine": r["sender_id"] == uid,
    } for r in rows]
    return jsonify({"messages": messages})


@app.route("/api/unread_count")
@login_required
def api_unread_count():
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) c FROM messages WHERE receiver_id = ? AND is_read = 0",
        (session["user_id"],),
    ).fetchone()["c"]
    return jsonify({"count": count})


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        init_db()
    else:
        # make sure tables exist even if the db file was created empty
        init_db()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
