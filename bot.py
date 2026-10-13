#!/usr/bin/env python3
"""
Iraq News Bot
Inoreader (folder "Iraq") -> keyword filter -> DeepSeek (Persian) -> Telegram channel

Uses only the Python standard library (no pip install), so each GitHub Actions
run stays fast and well under one billed minute.
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
TAG_CHECKED = "user/-/label/checked-iraq"  # every item the bot has handled
TAG_SENT = "user/-/label/sent-iraq"        # items actually posted to Telegram

LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS") or 3)
MAX_AI_PER_RUN = int(os.environ.get("MAX_AI_PER_RUN") or 40)
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN") or 20)
POST_GAP_SECONDS = float(os.environ.get("POST_GAP_SECONDS") or 2.5)
# Stop posting after this many seconds so a run stays near one billed minute.
# Relevant items not posted in time are kept for the next run.
TIME_BUDGET_SECONDS = float(os.environ.get("TIME_BUDGET_SECONDS") or 75)
MAX_PAGES = 3                               # up to 300 new items per run
DRY_RUN = (os.environ.get("DRY_RUN") or "false").lower() == "true"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"

KEYWORDS_FILE = Path(__file__).with_name("keywords.txt")

SYSTEM_PROMPT = """You are the news editor of a Persian-language Telegram channel that covers Iraq.
You receive one news item (any language: Arabic, English, Kurdish, Persian, ...).

1) Decide if the item is really about Iraq: Iraqi politics, government, security, economy,
   society, the Kurdistan Region of Iraq, Iraqi groups and figures, or Iraq's foreign relations.
   A passing mention of Iraq inside a story about something else is NOT relevant.
2) If relevant, write in fluent, formal, journalistic Persian (Farsi):
   - "title": a clear news headline, at most 15 words
   - "summary": 2 to 3 sentences
   - "key_points": 2 to 4 short bullet points
   Use standard Persian spellings of names and places. Use only facts present in the text.
   Do not add opinions or information that is not in the source.

Reply ONLY with a json object in exactly this shape:
{"relevant": true, "title": "...", "summary": "...", "key_points": ["...", "..."]}
If not relevant reply: {"relevant": false, "title": "", "summary": "", "key_points": []}"""


# ------------------------------------------------------------------ helpers
def need(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        sys.exit(f"ERROR: secret {name} is missing. Add it in GitHub > Settings > Secrets.")
    return value


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
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
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


def fetch_new_items(access):
    headers = {"Authorization": f"Bearer {access}"}
    since = int(time.time() - LOOKBACK_HOURS * 3600)
    stream = urllib.parse.quote(STREAM_ID, safe="")
    items, continuation = [], None
    for _ in range(MAX_PAGES):
        query = {"n": 100, "xt": TAG_CHECKED, "ot": since}
        if continuation:
            query["c"] = continuation
        url = f"{INOREADER}/reader/api/0/stream/contents/{stream}?{urllib.parse.urlencode(query)}"
        status, raw = http("GET", url, headers=headers)
        if status != 200:
            sys.exit(f"ERROR: could not read folder '{FOLDER}' ({status}): {raw[:300]}")
        data = json.loads(raw)
        items.extend(data.get("items", []))
        continuation = data.get("continuation")
        if not continuation:
            break
    return items


_BAD_IMAGE = re.compile(
    r"(pixel|tracking|feedburner|doubleclick|1x1|spacer|blank\.|logo|icon|avatar|emoji|"
    r"gravatar|\.svg|\.gif)", re.I)


def good_image(url):
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http") or _BAD_IMAGE.search(url):
        return ""
    return url


def image_from_feed(raw):
    for enc in raw.get("enclosure") or []:
        if str(enc.get("type", "")).startswith("image"):
            url = good_image(enc.get("href"))
            if url:
                return url
    content = (raw.get("summary") or {}).get("content", "")
    for tag in re.findall(r"<img\b[^>]*>", content, flags=re.I):
        m = re.search(r'\ssrc\s*=\s*["\']([^"\']+)', tag, flags=re.I)
        w = re.search(r'\swidth\s*=\s*["\']?(\d+)', tag, flags=re.I)
        if w and int(w.group(1)) < 100:
            continue  # tiny images are icons or trackers
        url = good_image(m.group(1)) if m else ""
        if url:
            return url
    return ""


def image_from_page(link):
    """Read og:image / twitter:image from the article page (first 300 KB only)."""
    if not link:
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
    return {
        "id": raw["id"],
        "title": strip_html(raw.get("title", "")),
        "text": strip_html((raw.get("summary") or {}).get("content", "")),
        "link": link,
        "source": strip_html((raw.get("origin") or {}).get("title", "")),
        "published": raw.get("published", 0),
        "image": image_from_feed(raw),
    }


def add_tag(access, ids, tag):
    headers = {"Authorization": f"Bearer {access}"}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = [("a", tag)] + [("i", item_id) for item_id in chunk]
        status, raw = http("POST", f"{INOREADER}/reader/api/0/edit-tag", data=data, headers=headers)
        if status != 200:
            print(f"WARNING: tagging failed ({status}): {raw[:200]}")


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
    status, raw = http("POST", DEEPSEEK_URL, data=payload, as_json=True, timeout=45,
                       headers={"Authorization": f"Bearer {need('DEEPSEEK_API_KEY')}"})
    if status != 200:
        raise RuntimeError(f"DeepSeek error ({status}): {raw[:200]}")
    content = json.loads(raw)["choices"][0]["message"]["content"]
    return content, json.loads(content)


def ask_deepseek(item):
    user_msg = (
        f"Source: {item['source']}\n"
        f"Headline: {item['title']}\n"
        f"Text: {item['text'][:3000]}\n\n"
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
        print("*** DRY RUN: nothing will be posted or tagged ***")

    patterns = load_keywords()
    access = get_access_token()

    items = [parse_item(x) for x in fetch_new_items(access)]
    unique = {it["id"]: it for it in items}
    items = sorted(unique.values(), key=lambda it: it["published"])  # oldest first

    candidates = [it for it in items if is_iraq_candidate(it, patterns)]
    not_iraq = [it for it in items if not is_iraq_candidate(it, patterns)]
    to_ai = candidates[:MAX_AI_PER_RUN]
    later = len(candidates) - len(to_ai)
    print(f"New items: {len(items)} | keyword match: {len(candidates)} | "
          f"to DeepSeek now: {len(to_ai)} | left for next run: {later}")

    results = []
    if to_ai:
        with ThreadPoolExecutor(max_workers=min(15, len(to_ai))) as pool:
            results = list(pool.map(ask_deepseek, to_ai))

    checked = [it["id"] for it in not_iraq]
    sent = []
    skipped = 0
    postponed = 0
    for item, ai in zip(to_ai, results):
        if ai is None:
            continue  # DeepSeek failed: try again next run
        if not ai.get("relevant") or not ai.get("title"):
            checked.append(item["id"])
            skipped += 1
            continue
        if len(sent) >= MAX_POSTS_PER_RUN or time.time() - started > TIME_BUDGET_SECONDS:
            postponed += 1  # not tagged: it will be posted in the next run
            continue
        post = build_post(item, ai, channel)
        if DRY_RUN:
            print("-" * 40 + f"\n[image: {item['image'] or 'none'}]\n" + post)
            sent.append(item["id"])
            continue
        result = send_telegram(post, item["image"])
        if result == "ok":
            sent.append(item["id"])
            checked.append(item["id"])
            time.sleep(POST_GAP_SECONDS)  # Telegram allows ~20 posts/minute per channel
        elif result == "bad":
            checked.append(item["id"])

    if not DRY_RUN:
        add_tag(access, checked, TAG_CHECKED)
        add_tag(access, sent, TAG_SENT)

    print(f"Posted: {len(sent)} | judged not about Iraq: {skipped} | "
          f"postponed to next run: {postponed} | time: {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
