# FlyCord

A VK/Twitter-style global broadcast feed, built with Flask + sqlite3.
Register a "node" (account), broadcast messages with `#hashtags`, photos,
or videos, like and reply to broadcasts, DM other users, subscribe to
channels, search the whole node, and watch it all update live without
ever refreshing the page.

## Run it locally

```bash
cd flycord
pip install -r requirements.txt
python app.py
```

(Or just double-click `run_flycord.bat` on Windows.)

The app starts on `http://localhost:5000`. The first run creates
`flycord.db` (sqlite) next to `app.py` — every account, broadcast, like,
comment, message, and channel lives in that one file, shared by anyone
who connects. Uploaded photos/videos land in `static/uploads/`.

## Expose it with ngrok

In a second terminal:

```bash
ngrok http 5000
```

Anyone who opens the printed `https://xxxx.ngrok-free.app` URL is hitting
your local Flask server and sharing the same database.

Before sharing the link widely:

- **This is a demo app, not hardened for the public internet.** No rate
  limiting, no email verification, no CSRF tokens. Set a real secret key:
  `export FLYCORD_SECRET="something-long-and-random"` before `python app.py`.
- Turn off Flask's debug mode (`debug=True` in `app.py`, last line) before
  exposing it publicly — its interactive debugger can allow remote code
  execution if someone finds it.
- Anyone with the link can register and post/DM.

## Admin account

The account named exactly **`Matan`** (capital M, case-sensitive) gets
admin powers: delete any broadcast, ban/unban accounts (for a chosen
duration or forever), delete a banned account entirely, and grant/revoke
the verified badge. Registration blocks anyone from creating a
same-letters-different-case lookalike (`matan`, `MATAN`, etc.) once
`Matan` exists, so there's no impersonation angle either. To change who's
admin, edit `ADMIN_USERNAMES` near the top of `app.py`.

## Verified badge & channels

Admins can grant any account a ✔️ **verified** badge from that user's
profile page. Verified accounts (and admins) get two extra abilities
regular accounts don't:

- **Creating brand-new `#tags`.** Anyone can *use* a tag that already
  exists, but only admins/verified accounts can be the first to use one —
  a regular user trying to post a never-before-seen tag gets a clear
  rejection message.
- **Creating channels** (from the "📢 Channels" nav link). Anyone can
  subscribe to any channel; subscribers get a "My Channels" tab in their
  Global Feed with everything posted into channels they follow. Channel
  posts still show up in the normal Global feed too — channels are a way
  to curate/follow specific creators, not a walled-off space. Only the
  channel's owner (or an admin) can delete it; deleting a channel doesn't
  delete its past posts, they just become ordinary broadcasts again.

## Live updates

The browser polls small JSON endpoints in the background and patches the
DOM:

- New broadcasts from other users fade into the Global Feed every ~3.5s
- Like/comment counts refresh every ~6s
- Discussion replies on a post appear every ~3s
- Direct-message chats poll every ~2s and auto-scroll
- The unread-messages badge updates every ~5s

Your own actions (posting, liking, replying, sending a DM) show up
instantly via AJAX — the polling is just for what everyone *else* is doing.

## Push notifications (new direct messages)

Turn this on from Settings → Notifications. When it's on, you get a real
OS-level notification when someone sends you a DM — even if FlyCord isn't
open in a tab. This uses the browser's Web Push API, not polling.

**On iPhone specifically**, Apple only allows a website to send push
notifications if it's been added to the Home Screen first — a regular
Safari tab can never receive them, no matter how correctly this (or any
site) implements it. To enable it on an iPhone:

1. Open the site in Safari (the ngrok link, or `http://localhost:5000` if
   you're on the same device as the server)
2. Tap the Share button → **Add to Home Screen**
3. Open FlyCord from that new Home Screen icon (not from Safari itself)
4. Go to Settings → turn on notifications there

Desktop Chrome/Edge/Firefox and Android Chrome don't need any of that —
the toggle in Settings just works directly in a normal browser tab.

The first time the app runs, it generates a VAPID key pair and saves it to
`vapid_private_key.pem` next to `app.py` — don't delete this file, or
everyone who's already turned notifications on will need to turn them on
again (their saved subscription becomes tied to keys that no longer match).

## Photo & video uploads

Only PNG/JPG (images) and MP4/MOV/WebM (video — MOV covers what iPhones
actually record video as) are accepted, 5MB/25MB size
caps respectively. Neither trusts the filename or browser-reported content
type — both check the file's actual bytes for a real image/video
signature, so a renamed file can't sneak through.

## What's implemented

- Register / log in / log out (passwords hashed with Werkzeug, 6-char min)
- Global Feed: composer with text, photo, or video (400-char text limit),
  `#hashtag` parsing → clickable/filterable tags, channel-posting picker
- Trending tags sidebar; Global All / My Broadcasts / My Channels tabs
- Likes and threaded Discussion replies, all live-updating
- Direct messages: inbox with unread counts, live chat threads, optional push notifications
- Channels: verified/admin-created, anyone can subscribe
- Search across usernames, bios, and broadcast text
- Explore page — directory of every registered user
- Profile pages with bio, avatar, Message button
- Admin: delete any broadcast, ban (timed or forever), delete banned
  accounts, grant/revoke verified badges
- Settings: change avatar/bio, change password, dark mode toggle
- Node Status widget with a live-polled ping + online user count
- Mobile: nav collapses, search bar and char-counter hide automatically;
  the nav strip also scrolls horizontally on any screen too narrow to
  fit every link (so nothing is ever hidden behind other elements)

## Project layout

```
flycord/
  app.py                     # routes, sqlite helpers, live-update APIs
  requirements.txt
  run_flycord.bat            # double-click launcher (Windows)
  flycord.db                  # created on first run
  static/uploads/              # created on first run (photos/videos)
  templates/
    base.html                 # nav, search bar
    feed.html, _post_card.html
    post.html                 # single broadcast + live discussion
    profile.html, explore.html, search.html, channels.html
    messages_inbox.html, message_thread.html
    login.html, register.html, settings.html
  static/
    css/style.css
    js/main.js                 # polling, AJAX, uploads
```
