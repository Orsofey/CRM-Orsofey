"""Публичные страницы для TikTok / Instagram / других платформ: условия использования и политика конфиденциальности.
Открываются без входа: {PUBLIC_URL}/terms и {PUBLIC_URL}/privacy. Контакт — переменная LEGAL_CONTACT (почта или t.me/…)."""
import html, os

UPDATED = "October 29, 2026"


def _contact() -> str:
    c = os.getenv("LEGAL_CONTACT", "").strip()
    if not c:
        return "through the Orsofey Telegram bot (send /start, then write your message)"
    href = c if c.startswith("http") else ("mailto:" + c if "@" in c and not c.startswith("@") else "https://t.me/" + c.lstrip("@"))
    return f"<a href='{html.escape(href)}'>{html.escape(c)}</a>"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Orsofey CRM</title>
<link rel="icon" href="/favicon.png"><style>
:root{{--bg:#eaf3ff;--card:#ffffffcc;--ink:#0f2a4a;--mute:#4a6688;--acc:#1f7cff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b1b33;--card:#13294acc;--ink:#e6f0ff;--mute:#9fb6d6;--acc:#5aa8ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;padding:24px 16px}}
main{{max-width:760px;margin:0 auto;background:var(--card);border-radius:22px;padding:28px 24px;
box-shadow:0 10px 30px rgba(31,92,170,.13);backdrop-filter:blur(12px)}}
h1{{margin:0 0 4px;font-size:28px}} h2{{font-size:19px;margin:26px 0 6px}} .muted{{color:var(--mute);font-size:14px}}
a{{color:var(--acc)}} li{{margin:4px 0}} nav{{margin-top:28px;font-size:14px}}
</style></head><body><main><h1>{title}</h1><div class="muted">Orsofey CRM · last updated {UPDATED}</div>
{body}<nav class="muted"><a href="/terms">Terms of Service</a> · <a href="/privacy">Privacy Policy</a></nav></main></body></html>"""


def terms() -> str:
    return _page("Terms of Service", f"""
<p>Orsofey CRM (“the Service”) is a dashboard for content creators run by Orsofey, a sole proprietor registered in Russia.
It shows a creator's own videos and their statistics from connected social networks (TikTok, YouTube, Instagram, VK)
next to the progress of people in the creator's Telegram bot, and helps prepare captions for cross-posting.</p>
<h2>1. Who can use it</h2>
<p>The Service is available only to people invited by Orsofey through the Telegram bot. You must be old enough to use
TikTok and the other connected platforms under their own rules.</p>
<h2>2. Connecting TikTok</h2>
<p>You connect your TikTok account yourself with “Log in with TikTok”. The Service only reads your basic profile and
your list of public videos with their statistics. It never posts, comments, likes or changes anything on your
TikTok account. You can disconnect at any time in the Service settings or in TikTok:
Settings and privacy → Security → Manage app permissions.</p>
<h2>3. Connecting Pinterest</h2>
<p>You connect your Pinterest account yourself with “Log in with Pinterest”. The Service reads your profile, boards,
your own Pins and their statistics (impressions, saves, clicks) to show them in your dashboard. It creates Pins only
when you publish or schedule a post in the Service yourself, on the board you choose. It never follows, comments,
saves other people's Pins or runs ads. You can disconnect at any time in the Service settings or in Pinterest:
Settings → Security and logins → Apps.</p>
<h2>4. Other platforms</h2>
<p>The same rules apply to the other platforms you connect (YouTube, Instagram, VK, Telegram, X, Bluesky, Discord,
Patreon, SubscribeStar, e621, Fur Affinity): the Service reads only your own content and statistics and publishes
only what you publish or schedule yourself.</p>
<p><b>Publishing on your behalf.</b> When you press «Allow publishing», you sign in to the platform (Google for YouTube,
TikTok, VK ID, X) and grant the Service permission to upload the posts you create in the Service to your own account.
The Service never posts anything you did not create or schedule, never deletes or edits your existing posts, and never
messages other users. YouTube publishing uses the <b>YouTube API Services</b>: by connecting YouTube you agree to the
<a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>, and Google's handling of data is described in the
<a href="https://policies.google.com/privacy">Google Privacy Policy</a>. You can revoke access at any time in the Service
(delete the account) or on the platform: Google — <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>,
TikTok — Settings → Security → Apps, VK — Settings → Apps, X — Settings → Security → Connected apps.
For Fur Affinity, which has no API, the Service uploads your artwork using the session cookie you provide, the same way
desktop uploaders do; delete the cookie in the Service or log out on Fur Affinity to revoke it.</p>
<h2>5. Your content</h2>
<p>Your videos and data stay yours. The Service only displays them to you (and to people you give access to your
dashboard) and does not claim any rights to them.</p>
<h2>6. Acceptable use</h2>
<p>Do not use the Service to break the law, the TikTok or Pinterest Terms of Service or the terms of other platforms, or to access
accounts that are not yours.</p>
<h2>7. No warranty</h2>
<p>The Service is provided “as is”. Statistics come from the platforms' APIs and may be delayed or incomplete.
Orsofey is not liable for indirect losses resulting from use of the Service.</p>
<h2>8. Changes and termination</h2>
<p>These terms may be updated; the date above shows the latest version. Orsofey may suspend access that violates
these terms. You may stop using the Service at any time.</p>
<h2>9. Contact</h2><p>Questions: {_contact()}.</p>""")


def privacy() -> str:
    return _page("Privacy Policy", f"""
<p>This policy explains what Orsofey CRM (“the Service”), operated by Orsofey (sole proprietor, Russia), collects
and how it is used.</p>
<h2>1. Data from TikTok</h2>
<ul><li><b>user.info.basic</b>: your TikTok open ID and display name — to show which account is connected.</li>
<li><b>video.list</b>: your public videos — ID, title/description, link, publish date, duration, and view, like,
comment and share counts — to show your content statistics.</li>
<li>Access and refresh tokens issued by TikTok — to update the statistics automatically every few hours.</li></ul>
<p>The Service does not request your password, private messages, followers' personal data, or permission to post.</p>
<h2>2. Data from Pinterest</h2>
<ul><li><b>user_accounts:read</b>: your username, follower count and monthly views — to show which account is connected and how it grows.</li>
<li><b>boards:read, pins:read</b>: your own boards and Pins — title, description, link, date and Pin statistics (impressions, saves, clicks).</li>
<li><b>boards:write, pins:write</b>: only to create the Pins you publish or schedule in the Service, on the board you choose.</li>
<li>Access and refresh tokens issued by Pinterest — to keep statistics up to date.</li></ul>
<p>The Service does not read other users' Pins or boards, does not access ads data, and does not sell or share Pinterest data.</p>
<h2>3. Data from other connected platforms</h2>
<p>For YouTube, Instagram, VK, Telegram, X, Bluesky, Discord, Patreon, SubscribeStar, e621 and Fur Affinity the Service
stores only what is needed to show your own content statistics and to publish what you ask: public post data and
counters, and the access tokens or keys you provide. For publishing it temporarily stores the media files and captions
of your posts (removed automatically after publishing, at most 21 days) and the access and refresh tokens the platform
issues. YouTube data obtained through the YouTube API Services (your channel ID, title and video statistics) is used
only to show your statistics and publish your videos, is refreshed at least every 30 days, and is deleted when you
remove the channel or revoke access.</p>
<h2>4. Data from the Telegram bot</h2>
<p>For people who use the Orsofey Telegram bot: Telegram user ID, name/username, the link or code word they came
from, and the funnel steps they completed (for example, receiving the gift or opening the download link).</p>
<h2>5. How data is used</h2>
<p>Only to show statistics and funnel progress inside the creator's own dashboard and in daily summaries sent to
the creator. Data is not sold, not used for advertising, and not shared with third parties, except the hosting
provider that runs the server (Amvera, Russia).</p>
<h2>6. Storage and security</h2>
<p>Data is stored in a database on the Service's server. Access to the dashboard requires a one-time login code from
the Telegram bot. Tokens are used only for requests to the platform that issued them.</p>
<h2>7. Retention and deletion</h2>
<p>Data is kept while the account is connected. When you disconnect an account in the Service settings, or revoke
access in the platform (TikTok: Settings and privacy → Security → Manage app permissions; Pinterest: Settings →
Security and logins → Apps), tokens stop working and no new data is
collected. To delete all stored data, contact us — it is removed within 30 days.</p>
<h2>8. Your rights</h2>
<p>You can ask what data is stored about you, ask to correct it, or ask to delete it.</p>
<h2>9. Contact</h2><p>{_contact()}.</p>""")
