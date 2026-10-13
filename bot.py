#!/usr/bin/env python3
"""
Iraq News Bot
Inoreader (folder "Iraq") -> keyword filter -> DeepSeek (Persian) -> Telegram channel

Uses only the Python standard library (no pip install).

Inoreader allows only 100 API calls a day per zone, so each run makes exactly ONE
Inoreader read call and NO write calls. What has been seen / is waiting to be posted
is kept in state.json, which GitHub Actions keeps between runs (actions/cache).
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ----------------------------------------------------------------- settings
INOREADER = "https://www.inoreader.com"
FOLDER = os.environ.get("INOREADER_FOLDER") or "Iraq"
STREAM_ID = f"user/-/label/{FOLDER}"
STATE_FILE = Path(os.environ.get("STATE_FILE") or "state.json")

FIRST_RUN_HOURS = float(os.environ.get("FIRST_RUN_HOURS") or 0.5)  # how far back on the very first run
OVERLAP_SECONDS = 900          # re-read the last 15 min each run so late items are not missed
QUEUE_MAX_AGE_HOURS = 8        # drop waiting items older than this
SEEN_KEEP_DAYS = 3
MAX_AI_PER_RUN = int(os.environ.get("MAX_AI_PER_RUN") or 40)
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN") or 20)
POST_GAP_SECONDS = float(os.environ.get("POST_GAP_SECONDS") or 2.5)
# Stop posting after this many seconds so a run stays near one billed minute.
# Relevant items not posted in time are kept for the next run.
TIME_BUDGET_SECONDS = float(os.environ.get("TIME_BUDGET_SECONDS") or 75)
DRY_RUN = (os.environ.get("DRY_RUN") or "false").lower() == "true"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-pro"

KEYWORDS_FILE = Path(__file__).with_name("keywords.txt")

SYSTEM_PROMPT = """You are the senior news editor of a Persian-language Telegram channel that covers Iraq.
You receive ONE news item: its headline, link and text (any language: Arabic, English, Kurdish, Persian...).

ACCURACY RULES (most important):
- The HEADLINE defines the story. Write only about that one story.
- The text may also contain other stories, menus, "related news", ads or page leftovers.
  Ignore everything in the text that is not about the headline's story.
- Use only facts that are in the source. Never add, guess or mix in information.
- If the text does not match the headline, use only what the headline says.

STEP 1: Is the story really about Iraq? (Iraqi politics, government, security, economy, society,
the Kurdistan Region of Iraq, Iraqi groups and figures, or Iraq's foreign relations.)
A passing mention of Iraq inside a story about something else is NOT relevant.

STEP 2: If relevant, write in fluent, natural, formal Persian, in the style of a professional
Iranian news agency (like IRNA or ISNA). This is not word-by-word translation: rewrite it as
good Persian journalism.
- "title": a clear Persian news headline, at most 15 words
- "summary": 2 to 3 Persian sentences: who, what, where, when
- "key_points": 2 to 4 short Persian bullet points with the most important facts
Persian style rules:
- Use Persian vocabulary and sentence order, not Arabic structure.
  Example: write "نخست‌وزیر عراق" not "رئیس الوزراء العراقی"; "اقلیم کردستان" not "اقليم كوردستان".
- Use the usual Persian forms of names: محمد شیاع السودانی، مسعود بارزانی، مقتدی صدر، آیت‌الله سیستانی،
  الحشد الشعبی، بغداد، اربیل، سلیمانیه، بصره، نجف، کربلا، موصل، کرکوک.
- Use Persian letters (ی، ک) and the half-space (‌) where needed (می‌شود، گفت‌وگو).
- Every field must be in Persian, never in Arabic, Kurdish or English.

Reply ONLY with a json object in exactly this shape:
{"relevant": true, "title": "...", "summary": "...", "key_points": ["...", "..."]}
If not relevant reply: {"relevant": false, "title": "", "summary": "", "key_points": []}"""


# ------------------------------------------------------------------ helpers
def need(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        sys.exit(f"ERROR: secret {name} is missing. Add it in GitHub > Settings > Secrets.")
    return value


LAST_HEADERS = {}


def http(method, url, data=None, headers=None, timeout=30, as_json=False):
    """Small HTTP helper. Returns (status_code, response_text)."""
    headers = dict(headers or {})
    body = None
    if data is not None:
        if as_json:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if "inoreader.com/reader" in url:
                LAST_HEADERS.clear()
                LAST_HEADERS.update({k.lower(): v for k, v in resp.headers.items()})
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if "inoreader.com/reader" in url:
            LAST_HEADERS.clear()
            LAST_HEADERS.update({k.lower(): v for k, v in e.headers.items()})
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # timeout, DNS, connection reset...
        return 0, str(e)


def strip_html(text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# Arabic / Persian / Kurdish letter normalisation so one keyword matches all spellings
_LETTERS = str.maketrans({
    "ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ە": "ه",
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
})
_MARKS = re.compile(r"[ً-ٰٟـ‌‏‎]")


def normalize(text):
    return _MARKS.sub("", (text or "").translate(_LETTERS)).lower()


# ------------------------------------------------------------------ keywords
def load_keywords():
    if not KEYWORDS_FILE.exists():
        sys.exit("ERROR: keywords.txt not found next to bot.py")
    patterns = []
    for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if not word or word.startswith("#"):
            continue
        word = normalize(word)
        if word.isascii():
            # Latin words: must start at a word boundary ("iraq" matches "Iraqi")
            patterns.append(re.compile(r"(?<![a-z])" + re.escape(word)))
        else:
            # Arabic-script words: plain substring (handles ال / و / ب prefixes)
            patterns.append(re.compile(re.escape(word)))
    print(f"Loaded {len(patterns)} keywords")
    return patterns


def is_iraq_candidate(item, patterns):
    text = normalize(item["title"] + " " + item["text"])
    return any(p.search(text) for p in patterns)


# ----------------------------------------------------------------- inoreader
def get_access_token():
    old_refresh = need("INOREADER_REFRESH_TOKEN")
    status, raw = http("POST", f"{INOREADER}/oauth2/token", data={
        "client_id": need("INOREADER_APP_ID"),
        "client_secret": need("INOREADER_APP_KEY"),
        "grant_type": "refresh_token",
        "refresh_token": old_refresh,
    })
    if status != 200:
        sys.exit(f"ERROR: Inoreader login failed ({status}): {raw[:300]}")
    data = json.loads(raw)
    access = data["access_token"]
    print(f"::add-mask::{access}")

    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != old_refresh:
        # Inoreader gave us a new refresh token: hand it to the next workflow step,
        # which stores it back into GitHub Secrets.
        print(f"::add-mask::{new_refresh}")
        out = Path(os.environ.get("RUNNER_TEMP") or ".") / "new_refresh_token.txt"
        out.write_text(new_refresh, encoding="utf-8")
        print("Inoreader refresh token was renewed; it will be saved to Secrets.")
    return access


def fetch_new_items(access, since):
    """ONE Inoreader call: up to 100 items added to the folder after `since`, oldest first.
    Returns (items, next_checkpoint) or (None, since) if Inoreader refused."""
    stream = urllib.parse.quote(STREAM_ID, safe="")
    query = {"n": 100, "r": "o", "ot": int(since)}
    url = f"{INOREADER}/reader/api/0/stream/contents/{stream}?{urllib.parse.urlencode(query)}"
    status, raw = http("GET", url, headers={"Authorization": f"Bearer {access}"})
    used, limit = LAST_HEADERS.get("x-reader-zone1-usage"), LAST_HEADERS.get("x-reader-zone1-limit")
    if used and limit:
        print(f"Inoreader API used today: {used}/{limit}")
    if status != 200:
        print(f"WARNING: could not read folder '{FOLDER}' ({status}): {raw[:300]}")
        if status == 429:
            print("WARNING: Inoreader daily API limit reached. Waiting items will still be posted.")
        return None, since
    items = json.loads(raw).get("items", [])
    if len(items) >= 100:
        # More items are waiting: continue next run from the last one we got
        newest = max(int(x.get("crawlTimeMsec") or 0) // 1000 for x in items)
        return items, max(newest, since + 1)
    return items, time.time()


_BAD_IMAGE = re.compile(
    r"(pixel|tracking|feedburner|doubleclick|1x1|spacer|blank\.|logo|icon|avatar|emoji|"
    r"gravatar|default|placeholder|fallback|no[-_]?image|share[-_]?image|banner|sprite|"
    r"\.svg|\.gif)", re.I)


def good_image(url):
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http") or _BAD_IMAGE.search(url):
        return ""
    return url


def image_from_feed(raw):
    """Only the image the feed attached to THIS item (enclosure).
    Images inside the feed text are not used: they often belong to other stories."""
    for enc in raw.get("enclosure") or []:
        if str(enc.get("type", "")).startswith("image"):
            url = good_image(enc.get("href"))
            if url:
                return url
    return ""


# Links that are not a single article page: their page image would be a logo or another story
_NOT_ARTICLE_HOSTS = ("news.google.", "google.com", "bing.com", "msn.com", "feedproxy.",
                      "feeds.", "rss.", "t.me/s/")


def is_article_link(link):
    if not link:
        return False
    parts = urllib.parse.urlparse(link)
    if any(h in (parts.netloc + parts.path) for h in _NOT_ARTICLE_HOSTS):
        return False
    path = parts.path.strip("/")
    return len(path) > 3 or bool(parts.query)  # homepage-like links are not articles


def image_from_page(link):
    """Read og:image / twitter:image from the article page (first 300 KB only)."""
    if not is_article_link(link):
        return ""
    req = urllib.request.Request(link, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            page = resp.read(300_000).decode("utf-8", "replace")
    except Exception:
        return ""
    for meta in re.findall(r"<meta\b[^>]*>", page, flags=re.I):
        if re.search(r'(property|name)\s*=\s*["\'](og:image|twitter:image)(:src)?["\']', meta, re.I):
            m = re.search(r'content\s*=\s*["\']([^"\']+)', meta, flags=re.I)
            url = good_image(urllib.parse.urljoin(link, html.unescape(m.group(1)))) if m else ""
            if url:
                return url
    return ""


def parse_item(raw):
    link = ""
    for key in ("canonical", "alternate"):
        if raw.get(key):
            link = raw[key][0].get("href", "")
            if link:
                break
    content = (raw.get("summary") or {}).get("content", "")
    text = strip_html(content)
    # Google News and other digest feeds put several different stories in one item:
    # then only the headline is trustworthy.
    if "news.google." in link or len(re.findall(r"<a\s", content, flags=re.I)) >= 4:
        text = ""
    return {
        "id": raw["id"],
        "title": strip_html(raw.get("title", "")),
        "text": text,
        "link": link,
        "source": strip_html((raw.get("origin") or {}).get("title", "")),
        "published": raw.get("published", 0),
        "image": image_from_feed(raw),
    }


# --------------------------------------------------------------------- state
def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        print(f"State loaded: {len(state.get('queue', []))} items waiting, "
              f"{len(state.get('seen', {}))} known items")
        return state
    except Exception:
        print(f"No saved state: first run, starting {FIRST_RUN_HOURS} hours back.")
        return {"checkpoint": time.time() - FIRST_RUN_HOURS * 3600, "seen": {}, "queue": []}


def save_state(state):
    now = time.time()
    state["seen"] = {k: v for k, v in state["seen"].items() if now - v < SEEN_KEEP_DAYS * 86400}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(f"State saved: {len(state['queue'])} items waiting for next run.")


# ------------------------------------------------------------------ deepseek
_PERSIAN_WORDS = re.compile(r"(^|\s)(است|این|را|که|از|به|با|برای|می|شد|شده|کرد|های|هایی|خود|نیز|اما)(\s|‌|$)")
_ARABIC_WORDS = re.compile(r"(^|\s)(في|على|التي|الذي|إلى|الى|هذا|هذه|عن|قد|كان|لم|ان|أن|وقال)(\s|$)")
_KURDISH_LETTERS = re.compile(r"[ەێۆڕڵ]")


def looks_persian(ai):
    """True if DeepSeek's answer is really in Persian (not Arabic, Kurdish or English)."""
    text = " ".join([str(ai.get("title", "")), str(ai.get("summary", ""))] +
                    [str(p) for p in ai.get("key_points") or []])
    letters = re.findall(r"[A-Za-z؀-ۿ]", text)
    if not letters:
        return False
    latin = sum(1 for c in letters if c.isascii())
    if latin / len(letters) > 0.25:
        return False                       # mostly English
    if len(_KURDISH_LETTERS.findall(text)) > 3:
        return False                       # Kurdish (Sorani)
    persian = len(_PERSIAN_WORDS.findall(text)) + len(re.findall(r"[پچژگ]", text))
    arabic = len(_ARABIC_WORDS.findall(text))
    return persian >= 2 and persian > arabic


def _deepseek_call(messages):
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 900,
    }
    status, raw = http("POST", DEEPSEEK_URL, data=payload, as_json=True, timeout=60,
                       headers={"Authorization": f"Bearer {need('DEEPSEEK_API_KEY')}"})
    if status != 200:
        raise RuntimeError(f"DeepSeek error ({status}): {raw[:200]}")
    content = json.loads(raw)["choices"][0]["message"]["content"]
    return content, json.loads(content)


def ask_deepseek(item):
    user_msg = (
        f"Source: {item['source']}\n"
        f"Headline: {item['title']}\n"
        f"Link: {item['link']}\n"
        f"Text: {item['text'][:3000] or '(no text, use the headline only)'}\n\n"
        "IMPORTANT: title, summary and key_points MUST be written in Persian (Farsi), "
        "never in Arabic, Kurdish or English, even if the source text is in that language."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    try:
        content, ai = _deepseek_call(messages)
        if ai.get("relevant") and not looks_persian(ai):
            # Second chance: ask DeepSeek to fix its own answer
            messages += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "Your answer is not in Persian. Translate title, summary "
                                            "and key_points into fluent Persian (Farsi) and reply "
                                            "with the same json shape."},
            ]
            content, ai = _deepseek_call(messages)
            if ai.get("relevant") and not looks_persian(ai):
                print(f"SKIP (not translated to Persian): {item['title'][:70]}")
                ai = {"relevant": False}
    except Exception as e:
        print(f"WARNING: DeepSeek failed for '{item['title'][:60]}': {e}")
        return None
    if ai.get("relevant") and not item["image"]:
        item["image"] = image_from_page(item["link"])
    return ai


# ------------------------------------------------------------------ telegram
def esc(text):
    return html.escape(str(text or "").strip(), quote=False)


def build_post(item, ai, channel):
    lines = [
        f"🔴 <b>{esc(ai.get('title'))}</b>",
        "",
        "📝 <b>خلاصه:</b>",
        esc(ai.get("summary")),
    ]
    points = [p for p in (ai.get("key_points") or []) if str(p).strip()]
    if points:
        lines += ["", "📌 <b>نکات کلیدی:</b>"]
        lines += [f"• {esc(p)}" for p in points[:5]]
    source = esc(item["source"]) or "لینک خبر"
    lines.append("")
    if item["link"]:
        lines.append(f'🔗 منبع: <a href="{html.escape(item["link"], quote=True)}">{source}</a>')
    else:
        lines.append(f"🔗 منبع: {source}")
    lines += ["", channel]
    return "\n".join(lines)


def _telegram(method, data):
    """Returns 'ok', 'bad' (Telegram refused it) or 'retry' (try next run)."""
    url = f"https://api.telegram.org/bot{need('TELEGRAM_BOT_TOKEN')}/{method}"
    for _ in range(3):
        status, raw = http("POST", url, data=data, timeout=30)
        if status == 200:
            return "ok"
        if status == 429:
            try:
                wait = json.loads(raw).get("parameters", {}).get("retry_after", 5)
            except Exception:
                wait = 5
            time.sleep(min(int(wait), 30))
            continue
        print(f"WARNING: Telegram {method} error ({status}): {raw[:300]}")
        return "bad" if status == 400 else "retry"
    return "retry"


def visible_length(post):
    return len(html.unescape(re.sub(r"<[^>]+>", "", post)))


def send_telegram(text, image=""):
    chat = need("TELEGRAM_CHANNEL")
    if image:
        if visible_length(text) <= 1024:
            # Photo with the post as its caption
            result = _telegram("sendPhoto", {
                "chat_id": chat, "photo": image, "caption": text, "parse_mode": "HTML"})
        else:
            # Too long for a caption: big image preview above the text
            result = _telegram("sendMessage", {
                "chat_id": chat, "text": text, "parse_mode": "HTML",
                "link_preview_options": json.dumps({
                    "url": image, "prefer_large_media": True, "show_above_text": True})})
        if result != "bad":
            return result
        print("Image rejected by Telegram, posting text only.")
    return _telegram("sendMessage", {
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True})})


# ---------------------------------------------------------------------- main
def main():
    started = time.time()
    for name in ("INOREADER_APP_ID", "INOREADER_APP_KEY", "INOREADER_REFRESH_TOKEN",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL", "DEEPSEEK_API_KEY"):
        need(name)
    channel = need("TELEGRAM_CHANNEL")
    if DRY_RUN:
        print("*** DRY RUN: nothing will be posted or saved ***")

    patterns = load_keywords()
    state = load_state()
    access = get_access_token()

    # 1. One Inoreader call for everything new since the last run
    since = state["checkpoint"] - OVERLAP_SECONDS
    raw_items, next_checkpoint = fetch_new_items(access, since)
    new_items = []
    for raw in raw_items or []:
        item_id = raw.get("id")
        if not item_id or item_id in state["seen"]:
            continue
        state["seen"][item_id] = time.time()
        new_items.append(parse_item(raw))
    candidates = [it for it in new_items if is_iraq_candidate(it, patterns)]
    for it in candidates:
        it["added"] = time.time()
        it["tries"] = 0
    state["queue"].extend(candidates)
    state["checkpoint"] = next_checkpoint

    # 2. Drop waiting items that are too old or keep failing
    now = time.time()
    queue = [it for it in state["queue"]
             if now - it.get("added", now) < QUEUE_MAX_AGE_HOURS * 3600 and it.get("tries", 0) < 3]
    queue.sort(key=lambda it: it.get("published", 0))  # oldest first
    print(f"New items: {len(new_items)} | keyword match: {len(candidates)} | "
          f"waiting in total: {len(queue)}")

    # 3. DeepSeek for waiting items that have not been checked yet (in parallel)
    to_ai = [it for it in queue if "ai" not in it][:MAX_AI_PER_RUN]
    if to_ai:
        print(f"Sending {len(to_ai)} items to DeepSeek ({DEEPSEEK_MODEL})")
        with ThreadPoolExecutor(max_workers=min(20, len(to_ai))) as pool:
            for item, ai in zip(to_ai, pool.map(ask_deepseek, to_ai)):
                if ai is None:
                    item["tries"] = item.get("tries", 0) + 1   # try again next run
                else:
                    item["ai"] = ai

    # 4. Post the confirmed items, oldest first
    keep, sent, skipped, postponed = [], 0, 0, 0
    for item in queue:
        ai = item.get("ai")
        if ai is None:
            keep.append(item)            # not checked yet
            continue
        if not ai.get("relevant") or not ai.get("title"):
            skipped += 1                 # not about Iraq: forget it
            continue
        if sent >= MAX_POSTS_PER_RUN or time.time() - started > TIME_BUDGET_SECONDS:
            postponed += 1
            keep.append(item)            # post next run
            continue
        post = build_post(item, ai, channel)
        if DRY_RUN:
            print("-" * 40 + f"\n[link: {item['link']}]\n[image: {item['image'] or 'none'}]\n" + post)
            sent += 1
            continue
        print(f"POST | {item['source']} | {item['title'][:70]} | link: {item['link']} | "
              f"image: {item['image'] or 'none'}")
        result = send_telegram(post, item["image"])
        if result == "ok":
            sent += 1
            time.sleep(POST_GAP_SECONDS)  # Telegram allows ~20 posts/minute per channel
        elif result == "retry":
            item["tries"] = item.get("tries", 0) + 1
            keep.append(item)

    state["queue"] = keep
    if DRY_RUN:
        print("DRY RUN: state not saved, nothing posted.")
    else:
        save_state(state)

    print(f"Posted: {sent} | judged not about Iraq: {skipped} | "
          f"postponed to next run: {postponed} | time: {time.time() - started:.1f}s")

if __name__ == "__main__":
    main()
