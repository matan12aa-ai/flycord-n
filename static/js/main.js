(function () {
  const FEED_POLL_MS = 3500;
  const COUNTS_POLL_MS = 6000;
  const COMMENT_POLL_MS = 3000;
  const CHAT_POLL_MS = 2000;
  const UNREAD_POLL_MS = 5000;

  // Matches GUEST_AVATAR_SVG in app.py — used here for posts/comments that
  // stream in live (via polling) from someone with no uploaded picture.
  const GUEST_AVATAR_SVG =
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
    + '<circle cx="12" cy="8" r="4" fill="currentColor"/>'
    + '<path d="M4 20c0-4.4 3.6-8 8-8s8 3.6 8 8" fill="currentColor"/>'
    + '</svg>';

  function avatarHtml(url, sizeClass) {
    return url
      ? `<img class="${sizeClass} avatar-img" src="${url}" alt="">`
      : `<span class="${sizeClass} avatar-guest">${GUEST_AVATAR_SVG}</span>`;
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function fetchJSON(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, opts.headers || {});
    return fetch(url, opts).then((r) => (r.ok ? r.json() : Promise.reject(r)));
  }

  // ---------------------------------------------------------------------
  // Dark mode
  //
  // The *initial* theme on every page load is rendered server-side —
  // Flask reads the flycord_theme cookie and bakes class="dark" directly
  // into the <html> tag it sends (see app.py's inject_user + base.html).
  // That's what actually prevents the flash: it's correct from the first
  // byte of HTML, no client-side JS race involved. For that to work, this
  // toggle has to set the COOKIE, not just localStorage — localStorage
  // alone was the bug: it can't be read server-side, so every fresh page
  // load kept falling back to the (less reliable, especially on mobile)
  // client-side script instead of the bulletproof server-rendered class.
  // ---------------------------------------------------------------------
  window.toggleTheme = function () {
    const isDark = document.documentElement.classList.toggle('dark');
    const theme = isDark ? 'dark' : 'light';
    try { localStorage.setItem('flycord-theme', theme); } catch (e) { /* private browsing, etc. */ }
    document.cookie = `flycord_theme=${theme}; path=/; max-age=31536000; samesite=lax`;
    const sw = document.getElementById('dark-mode-switch');
    if (sw) sw.checked = isDark;
  };

  // ---------------------------------------------------------------------
  // Post card builder (mirrors templates/_post_card.html) — used to inject
  // freshly-polled broadcasts without a page reload.
  // ---------------------------------------------------------------------
  function buildPostCard(post) {
    const card = document.createElement('div');
    card.className = 'card post-card new-post-flash';
    card.dataset.postId = post.id;

    const likeIcon = post.liked ? '❤️' : '🤍';
    const adminBadge = post.is_admin_author
      ? '<span class="admin-badge" title="Admin">🛡️ Admin</span>'
      : (post.is_verified_author ? '<span class="verified-badge" title="Verified">✔️ Verified</span>' : '');
    const deleteBtn = post.can_delete ? `
        <form method="post" action="/post/${post.id}/delete" class="inline-form delete-form">
          <button type="submit" class="link-btn delete-btn">
            ${(post.is_admin_author === false && !post.is_mine) ? '🛡️ Admin delete' : '🗑️ Delete'}
          </button>
        </form>` : '';

    // Channel posts show the channel's own identity (name/picture), never
    // the underlying account that posted it — channel creators stay
    // anonymous, same as the server-rendered template does.
    const header = post.channel_name ? `
      <a class="post-header" href="/channels">
        ${post.channel_image_url
          ? `<img class="avatar-sm channel-avatar-img" src="${post.channel_image_url}" alt="">`
          : '<span class="avatar-sm">📢</span>'}
        <span class="post-username">${escapeHtml(post.channel_name)}</span>
        <span class="channel-badge" title="Channel">📢 Channel</span>
        <span class="post-time">· just now</span>
      </a>` : `
      <a class="post-header" href="/u/${encodeURIComponent(post.username)}">
        ${avatarHtml(post.avatar_url, 'avatar-sm')}
        <span class="post-username">${escapeHtml(post.username)}</span>
        ${adminBadge}
        <span class="post-time">· just now</span>
      </a>`;

    card.innerHTML = `
      ${header}
      ${post.content_html ? `<div class="post-content">${post.content_html}</div>` : ''}
      ${post.image_url ? `<div class="post-image"><img src="${post.image_url}" alt="Broadcast image" loading="lazy"></div>` : ''}
      ${post.video_url ? `<div class="post-video"><video controls preload="metadata" src="${post.video_url}"></video></div>` : ''}
      <div class="post-footer">
        <form method="post" action="/post/${post.id}/like" class="inline-form like-form">
          <button type="submit" class="link-btn like-btn ${post.liked ? 'liked' : ''}">
            <span class="like-icon">${likeIcon}</span>
            <span class="like-count">${post.like_count}</span>
          </button>
        </form>
        <a class="link-btn" href="/post/${post.id}">
          💬 Discussion (<span class="comment-count">${post.comment_count}</span>)
        </a>
        ${deleteBtn}
      </div>
    `;
    bindLikeForm(card.querySelector('.like-form'));
    bindDeleteForm(card.querySelector('.delete-form'));
    return card;
  }

  // ---------------------------------------------------------------------
  // Live feed polling — new broadcasts appear without a refresh
  // ---------------------------------------------------------------------
  function initFeedLivePoll() {
    const list = document.getElementById('post-list');
    if (!list) return;

    const view = list.dataset.view || 'all';
    const tag = list.dataset.tag || '';

    function poll() {
      const lastId = list.dataset.lastId || 0;
      const params = new URLSearchParams({ since_id: lastId, view: view, tag: tag });
      fetchJSON('/api/feed/updates?' + params.toString())
        .then((data) => {
          if (!data.posts || !data.posts.length) return;
          const emptyState = document.getElementById('empty-state');
          if (emptyState) emptyState.remove();

          // API returns oldest-first among the new batch; insert each at top
          data.posts.forEach((post) => {
            if (list.querySelector(`[data-post-id="${post.id}"]`)) return;
            list.prepend(buildPostCard(post));
            list.dataset.lastId = Math.max(Number(list.dataset.lastId || 0), post.id);
          });
        })
        .catch(() => {});
    }

    function pollCounts() {
      const ids = Array.from(list.querySelectorAll('.post-card')).map((el) => el.dataset.postId);
      if (!ids.length) return;
      fetchJSON('/api/feed/counts?ids=' + ids.join(','))
        .then((data) => {
          Object.entries(data.counts || {}).forEach(([id, c]) => {
            const card = list.querySelector(`[data-post-id="${id}"]`);
            if (!card) return;
            const likeCountEl = card.querySelector('.like-count');
            const commentCountEl = card.querySelector('.comment-count');
            if (likeCountEl) likeCountEl.textContent = c.like_count;
            if (commentCountEl) commentCountEl.textContent = c.comment_count;
          });
        })
        .catch(() => {});
    }

    setInterval(poll, FEED_POLL_MS);
    setInterval(pollCounts, COUNTS_POLL_MS);
  }

  // ---------------------------------------------------------------------
  // AJAX like button — works from the feed, a profile, search, or a
  // single-post page. Falls back to a normal form submit if fetch fails.
  // ---------------------------------------------------------------------
  function bindLikeForm(form) {
    if (!form || form.dataset.bound) return;
    form.dataset.bound = '1';
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      fetchJSON(form.action, { method: 'POST' })
        .then((data) => {
          const btn = form.querySelector('.like-btn');
          const icon = form.querySelector('.like-icon');
          const count = form.querySelector('.like-count');
          btn.classList.toggle('liked', data.liked);
          icon.textContent = data.liked ? '❤️' : '🤍';
          count.textContent = data.like_count;
        })
        .catch(() => { form.submit(); });
    });
  }

  function bindAllLikeForms() {
    document.querySelectorAll('.like-form').forEach(bindLikeForm);
  }

  // ---------------------------------------------------------------------
  // AJAX delete button (owner or admin). Confirms first, then removes the
  // card from the DOM — or, on a single-post page, bounces back to the feed.
  // ---------------------------------------------------------------------
  function bindDeleteForm(form) {
    if (!form || form.dataset.bound) return;
    form.dataset.bound = '1';
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if (!window.confirm('Delete this broadcast? This cannot be undone.')) return;
      fetchJSON(form.action, { method: 'POST' })
        .then(() => {
          const card = form.closest('.post-card');
          if (!card) return;
          const inFeedList = card.closest('#post-list');
          card.remove();
          if (!inFeedList) {
            window.location.href = '/';
          }
        })
        .catch(() => { form.submit(); });
    });
  }

  function bindAllDeleteForms() {
    document.querySelectorAll('.delete-form').forEach(bindDeleteForm);
  }

  // ---------------------------------------------------------------------
  // Post-detail page: live comment polling + AJAX reply submit
  // ---------------------------------------------------------------------
  function buildCommentEl(c) {
    const el = document.createElement('div');
    el.className = 'comment';
    el.dataset.commentId = c.id;
    const adminBadge = c.is_admin_author
      ? '<span class="admin-badge" title="Admin">🛡️ Admin</span>'
      : (c.is_verified_author ? '<span class="verified-badge" title="Verified">✔️ Verified</span>' : '');
    el.innerHTML = `
      ${avatarHtml(c.avatar_url, 'avatar-sm')}
      <div class="comment-body">
        <div class="comment-meta">
          <strong>${escapeHtml(c.username)}</strong>
          ${adminBadge}
          <span class="post-time">${c.time_label}</span>
        </div>
        <div class="comment-text">${c.content_html}</div>
      </div>
    `;
    return el;
  }

  function initCommentLivePoll() {
    const list = document.getElementById('comment-list');
    if (!list) return;

    function poll() {
      const lastId = list.dataset.lastId || 0;
      fetchJSON(`/api/post/${list.dataset.postId}/comments/updates?since_id=${lastId}`)
        .then((data) => {
          if (!data.comments || !data.comments.length) return;
          const noComments = document.getElementById('no-comments');
          if (noComments) noComments.remove();
          data.comments.forEach((c) => {
            if (list.querySelector(`[data-comment-id="${c.id}"]`)) return;
            list.appendChild(buildCommentEl(c));
            list.dataset.lastId = Math.max(Number(list.dataset.lastId || 0), c.id);
          });
        })
        .catch(() => {});
    }

    const form = document.getElementById('comment-form');
    if (form) {
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        const input = form.querySelector('input[name="content"]');
        const content = input.value.trim();
        if (!content) return;
        fetchJSON(form.action, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'content=' + encodeURIComponent(content),
        })
          .then((c) => {
            const noComments = document.getElementById('no-comments');
            if (noComments) noComments.remove();
            list.appendChild(buildCommentEl(c));
            list.dataset.lastId = Math.max(Number(list.dataset.lastId || 0), c.id);
            input.value = '';
          })
          .catch(() => { form.submit(); });
      });
    }

    setInterval(poll, COMMENT_POLL_MS);
  }

  // ---------------------------------------------------------------------
  // Direct messages: live chat polling + AJAX send
  // ---------------------------------------------------------------------
  function buildBubble(m, mine) {
    const el = document.createElement('div');
    el.className = 'chat-bubble ' + (mine ? 'mine' : 'theirs');
    el.dataset.msgId = m.id;
    el.innerHTML = `
      ${m.image_url ? `<div class="bubble-media"><img src="${m.image_url}" alt="Photo" loading="lazy"></div>` : ''}
      ${m.video_url ? `<div class="bubble-media"><video controls preload="metadata" src="${m.video_url}"></video></div>` : ''}
      ${m.content ? `<div class="bubble-text">${escapeHtml(m.content)}</div>` : ''}
      <div class="bubble-time">just now</div>
    `;
    return el;
  }

  function initChat() {
    const box = document.getElementById('chat-messages');
    if (!box) return;
    const partner = box.dataset.partner;

    function scrollToBottom() {
      box.scrollTop = box.scrollHeight;
    }
    scrollToBottom();

    function poll() {
      const lastId = box.dataset.lastId || 0;
      fetchJSON(`/api/messages/${encodeURIComponent(partner)}/updates?since_id=${lastId}`)
        .then((data) => {
          if (!data.messages || !data.messages.length) return;
          const noMsg = document.getElementById('no-messages');
          if (noMsg) noMsg.remove();
          data.messages.forEach((m) => {
            if (box.querySelector(`[data-msg-id="${m.id}"]`)) return;
            box.appendChild(buildBubble(m, m.is_mine));
            box.dataset.lastId = Math.max(Number(box.dataset.lastId || 0), m.id);
          });
          scrollToBottom();
        })
        .catch(() => {});
    }

    const form = document.getElementById('chat-form');
    const input = document.getElementById('chat-input');
    const imageInput = document.getElementById('chat-image-input');
    const videoInput = document.getElementById('chat-video-input');

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const content = input.value.trim();
      const hasImage = imageInput && imageInput.files && imageInput.files.length;
      const hasVideo = videoInput && videoInput.files && videoInput.files.length;
      if (!content && !hasImage && !hasVideo) return;

      const formData = new FormData();
      formData.append('content', content);
      if (hasImage) formData.append('image', imageInput.files[0]);
      if (hasVideo) formData.append('video', videoInput.files[0]);

      fetchJSON(form.action, { method: 'POST', body: formData })
        .then((m) => {
          const noMsg = document.getElementById('no-messages');
          if (noMsg) noMsg.remove();
          box.appendChild(buildBubble(m, true));
          box.dataset.lastId = Math.max(Number(box.dataset.lastId || 0), m.id);
          input.value = '';
          clearMediaInput('chat-image-input');
          clearMediaInput('chat-video-input');
          scrollToBottom();
        })
        .catch(() => { form.submit(); });
    });

    setInterval(poll, CHAT_POLL_MS);
  }

  // ---------------------------------------------------------------------
  // Photo/video picker for the composer. Shows the chosen filename as
  // plain text (no thumbnail) so there's nothing that can render as a
  // broken-image icon. The real validation (file type, size, actual file
  // content) happens server-side — this is just fast feedback + UX.
  // Only one attachment at a time: picking a photo clears any selected
  // video, and picking a video clears any selected photo.
  // ---------------------------------------------------------------------
  const ALLOWED_IMAGE_TYPES = ['image/png', 'image/jpeg'];
  const ALLOWED_IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg'];
  const ALLOWED_VIDEO_TYPES = ['video/mp4', 'video/webm', 'video/quicktime'];
  const ALLOWED_VIDEO_EXTENSIONS = ['.mp4', '.webm', '.mov'];

  function isAllowedFile(file, allowedTypes, allowedExtensions) {
    if (allowedTypes.includes(file.type)) return true;
    // Fall back to the filename's extension — iOS Safari in particular
    // doesn't always report a MIME type (or reports an unexpected one)
    // for .mov files depending on how they were picked. The server does
    // the real, authoritative check on file content either way.
    const name = file.name.toLowerCase();
    return allowedExtensions.some((ext) => name.endsWith(ext));
  }

  function clearMediaInput(inputId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.value = '';
    const prefix = inputId.replace(/-input$/, '');
    const preview = document.getElementById(prefix + '-preview');
    if (preview) preview.hidden = true;
  }

  function initMediaPicker(triggerClass, allowedTypes, allowedExtensions, errorMessage, otherInputId) {
    document.querySelectorAll(triggerClass).forEach((btn) => {
      const input = document.getElementById(btn.dataset.target);
      if (!input) return;
      const prefix = btn.dataset.target.replace(/-input$/, '');
      const preview = document.getElementById(prefix + '-preview');
      const filenameEl = document.getElementById(prefix + '-filename');

      btn.addEventListener('click', () => input.click());

      input.addEventListener('change', () => {
        const file = input.files && input.files[0];
        if (!file) return;
        if (!isAllowedFile(file, allowedTypes, allowedExtensions)) {
          alert(errorMessage);
          input.value = '';
          if (preview) preview.hidden = true;
          return;
        }
        if (otherInputId) clearMediaInput(otherInputId);
        if (filenameEl) filenameEl.textContent = '📎 ' + file.name;
        if (preview) preview.hidden = false;
      });
    });
  }

  function initImagePicker() {
    initMediaPicker(
      '.image-trigger', ALLOWED_IMAGE_TYPES, ALLOWED_IMAGE_EXTENSIONS,
      'Only PNG and JPG images are allowed.', 'composer-video-input'
    );
    initMediaPicker(
      '.video-trigger', ALLOWED_VIDEO_TYPES, ALLOWED_VIDEO_EXTENSIONS,
      'Only MP4, MOV, and WebM videos are allowed.', 'composer-image-input'
    );
  }

  // ---------------------------------------------------------------------
  // Unread messages badge (polled globally on every page once logged in)
  // ---------------------------------------------------------------------
  function initUnreadBadge() {
    const badges = [
      document.getElementById('unread-badge'),
      document.getElementById('unread-badge-mobile'),
    ].filter(Boolean);
    if (!badges.length) return;

    function poll() {
      fetchJSON('/api/unread_count')
        .then((data) => {
          badges.forEach((badge) => {
            if (data.count > 0) {
              badge.textContent = data.count > 99 ? '99+' : data.count;
              badge.hidden = false;
            } else {
              badge.hidden = true;
            }
          });
        })
        .catch(() => {});
    }
    poll();
    setInterval(poll, UNREAD_POLL_MS);
  }

  // ---------------------------------------------------------------------
  // Node status live-ish ping
  // ---------------------------------------------------------------------
  function initNodeStatus() {
    const pingEl = document.getElementById('ping-value');
    if (!pingEl) return;
    const usersEl = document.getElementById('users-value');

    function refresh() {
      fetchJSON('/api/status')
        .then((data) => {
          pingEl.textContent = data.ping + 'ms';
          if (usersEl) usersEl.textContent = data.users;
        })
        .catch(() => {});
    }
    refresh();
    setInterval(refresh, 4000);
  }

  // ---------------------------------------------------------------------
  // Web push notifications — notifies the recipient (if they've opted in)
  // when someone sends them a direct message. Uses the browser's native
  // Push API + a service worker (static/sw.js), no polling involved for
  // the notification itself.
  //
  // Important iOS caveat, unavoidable on Apple's end: Safari only supports
  // web push for a site that's been "Added to Home Screen" (iOS 16.4+) —
  // a regular Safari tab can never receive push notifications, no matter
  // how correctly everything here is implemented. The toggle below detects
  // that case and explains it instead of silently failing.
  // ---------------------------------------------------------------------
  function pushSupported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  }

  function isIos() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent);
  }

  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; i++) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  }

  function getExistingSubscription() {
    if (!pushSupported()) return Promise.resolve(null);
    return navigator.serviceWorker.register('/sw.js')
      .then((reg) => reg.pushManager.getSubscription())
      .catch(() => null);
  }

  function subscribeToPush() {
    return navigator.serviceWorker.register('/sw.js')
      .then((reg) => fetchJSON('/push/vapid-public-key')
        .then((data) => reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(data.key),
        })))
      .then((subscription) => fetchJSON('/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(subscription.toJSON()),
      }).then(() => subscription));
  }

  function unsubscribeFromPush(subscription) {
    return subscription.unsubscribe().then(() =>
      fetchJSON('/push/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: subscription.endpoint }),
      })
    );
  }

  function initPushToggle() {
    const toggle = document.getElementById('push-toggle');
    const hint = document.getElementById('push-status-hint');
    if (!toggle || !hint) return;

    if (!pushSupported()) {
      toggle.disabled = true;
      hint.textContent = "Your browser doesn't support push notifications.";
      return;
    }

    if (isIos() && !isStandalone()) {
      toggle.disabled = true;
      hint.textContent = 'On iPhone: tap Share → "Add to Home Screen" first, then open FlyCord from that Home Screen icon to turn this on. A regular Safari tab can\'t receive notifications — that\'s an iOS restriction, not a FlyCord one.';
      return;
    }

    if (Notification.permission === 'denied') {
      toggle.disabled = true;
      hint.textContent = 'Notifications are blocked for this site in your browser/OS settings — enable them there first.';
      return;
    }

    hint.textContent = "You'll get a notification when someone sends you a direct message.";

    getExistingSubscription().then((sub) => {
      toggle.checked = !!sub;
    });

    toggle.addEventListener('change', () => {
      toggle.disabled = true;
      if (toggle.checked) {
        subscribeToPush()
          .then(() => { hint.textContent = "Notifications on — you'll be alerted about new direct messages."; })
          .catch(() => {
            toggle.checked = false;
            hint.textContent = "Couldn't enable notifications — permission may have been denied.";
          })
          .finally(() => { toggle.disabled = false; });
      } else {
        getExistingSubscription()
          .then((sub) => (sub ? unsubscribeFromPush(sub) : null))
          .then(() => { hint.textContent = "Notifications off. You'll get a notification when someone sends you a direct message."; })
          .finally(() => { toggle.disabled = false; });
      }
    });
  }

  window.FlyCord = { initFeedLivePoll, initCommentLivePoll, initChat, initPushToggle };

  document.addEventListener('DOMContentLoaded', () => {
    const themeBtn = document.getElementById('theme-toggle');
    if (themeBtn) themeBtn.addEventListener('click', window.toggleTheme);

    initNodeStatus();
    initUnreadBadge();
    initImagePicker();
    bindAllLikeForms();
    bindAllDeleteForms();
  });
})();
