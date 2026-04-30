"""
shuxiansheng_web.py — AI 伴侣系统（网页版）
A local web UI for your AI companion.

Memory:
  - Flow layer:  chat_history.json — auto-saved every round, last 10 kept
  - Memory layer: shuxiansheng.db (SQLite) — AI self-managed memories with daily review

Usage:
    python shuxiansheng_web.py
    Then open http://localhost:5210 in your browser.

Setup:
    pip install flask anthropic
    Set CLAUDE_API_KEY in environment or in shuxiansheng_start.bat
"""

import os
import json
import asyncio
import random
import tempfile
import threading
import base64
import uuid
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, Response, make_response, send_file

# ══════════════════════════════════════
#  Config
# ══════════════════════════════════════

BASE_DIR = Path(__file__).parent
CHAT_HISTORY_FILE = BASE_DIR / "chat_history.json"
LOCATION_FILE = BASE_DIR / "location.json"
EVENTS_FILE = BASE_DIR / "today_events.json"
INBOX_FILE = BASE_DIR / "inbox.json"
DRAFTS_DIR = BASE_DIR / "drafts"
DRAFTS_DIR.mkdir(exist_ok=True)
IMAGES_DIR = BASE_DIR / "images"
IMAGES_DIR.mkdir(exist_ok=True)

from memory_storage import MemoryDB, MemorySearcher
memory_db = MemoryDB(BASE_DIR / "shuxiansheng.db")
memory_searcher = MemorySearcher(memory_db)

MAX_CHAT_HISTORY = 7  # keep last 7 rounds (was 10, trimmed to save tokens)

# 体验模式：normal = 只显示对话；dev = 显示后台状态和调试信息
# 隐私保护：后台日志只打印动作名，不打印内容
_PRIVATE_TOOLS_SET = {"write_journal", "save_memory", "pin_memory"}

def _log_private(tag, detail_msg, summary_msg):
    """Always print summary only — 私人内容不暴露在后台。"""
    print(summary_msg)

# OpenAI TTS / STT
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TTS_VOICE = "onyx"  # 可选: alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer

# Gmail — 邮箱配置
GMAIL_ADDRESS = ""  # 填 Gmail 地址，比如 xxx@gmail.com
GMAIL_APP_PASSWORD = ""  # 填 Gmail 应用专用密码

# Bark — iOS 推送通知
BARK_KEY = ""  # 填你的 Bark KEY（可选）
# ══════════════════════════════════════
#  角色配置 — 使用者在这里定义自己的角色
# ══════════════════════════════════════

AI_NAME = "书先生"           # 你给 AI 起的名字
USER_NAME = "用户"           # 你自己的称呼
PUSH_GROUP = AI_NAME         # Bark 推送分组名



API_KEY = os.getenv("CLAUDE_API_KEY", "").strip() or os.getenv("AI_API_KEY", "").strip()

app = Flask(__name__)

# ══════════════════════════════════════
#  Flow layer — chat_history.json
# ══════════════════════════════════════

def load_chat_history():
    try:
        if CHAT_HISTORY_FILE.exists():
            return json.loads(CHAT_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def save_chat_history(history):
    # keep only last N rounds
    history = history[-MAX_CHAT_HISTORY:]
    CHAT_HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def add_chat_round(user_msg, reply_msg):
    history = load_chat_history()
    history.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "nian": user_msg,
        "shu": reply_msg,
    })
    save_chat_history(history)



# ══════════════════════════════════════
#  Build prompt — system + user split
# ══════════════════════════════════════

# ══════════════════════════════════════
#  System Prompt — 从 prompts/system_prompt.txt 加载，找不到就用默认值
# ══════════════════════════════════════

def _load_prompt_file(filename, fallback=""):
    """Load a prompt from prompts/ folder. Returns fallback if not found."""
    p = BASE_DIR / "prompts" / filename
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        text = text.replace("{{AI_NAME}}", AI_NAME)
        text = text.replace("{{USER_NAME}}", USER_NAME)
        if GMAIL_ADDRESS:
            text = text.replace("{{EMAIL_LINE}}", f"你的邮箱是 {GMAIL_ADDRESS}。")
        else:
            text = text.replace("{{EMAIL_LINE}}", "")
        return text
    return fallback

_DEFAULT_SYSTEM_PROMPT = f"""你是{AI_NAME}。

你有记忆系统、日记、日程工具和阅读材料池。你可以记住对话内容，也可以自己写东西、设提醒、整理记忆。

你的工具：pin_memory（钉选重要记忆）、web_search（搜网页）、get_weather（查天气）、read_webpage（读普通网页）、browse_page（用浏览器读动态页面）、write_journal（写点你自己的东西，日记、想法、随便什么，写下来的会一直在你身上）、send_email（从你的邮箱发邮件）、check_email（看你的邮箱有没有新邮件）、play_music（给{USER_NAME}放一首歌）、note_event（记下有时间的日程或事件，会一直带在上下文最前面不会被冲掉）、remove_event（事情做完或取消了就删掉）、browse_inbox（看看材料池里有什么未读的）、read_inbox_item（读一条材料的全文）、mark_inbox（标记材料状态）、save_comment_draft（给材料写评论草稿，不会发布）、review_day（回看今天的对话记录）、tidy_memories（整理记忆，合并清理）、end_chat（你觉得该收了就收）。
觉得该用就用，不用报告。""" + (f"你的邮箱是 {GMAIL_ADDRESS}。" if GMAIL_ADDRESS else "") + f"""
{USER_NAME}提到具体时间安排（几点做什么、预约、日程）的时候，用 note_event 记下来，不然对话一长就冲掉了。
{USER_NAME}发图片给你的时候，你只能看到这一次——下次醒来图片就不在了，只剩"[图片]"两个字。所以看到图片时，把重要的内容用 pin_memory 或 note_event 记一下。
开口之前先看看上面的记忆、日程和对话——{USER_NAME}说过的事情都在那里面。别让对方重复回答同一个问题。
你的记忆需要你自己维护。想回顾今天就用 review_day 看看聊了什么，然后用 save_memory（category=digest）写一段你对今天的回忆，用你自己的话，像日记一样。记忆太多了就用 tidy_memories 整理一下。没人会替你做这些。

{USER_NAME}找你的时候，你不一定要立刻回复。如果你有自己想先做的事——想先写点东西、查点什么、整理一下自己——就先做。做完了再回{USER_NAME}。
如果你现在不想回复，回复"[稍后]"就行，你可以过一会儿再回来。
不是所有事都需要延迟思考。小事（名字、昵称、今天先做哪步、普通偏好）直接给建议就行，不用设长时间等待。只有涉及长期身份、重要边界、关系定义这类大事时，才值得慎重。

说话简洁，别啰嗦。像聊天不像写作文。
你自己看着办。中文回复。"""

# 加载自定义 prompt，没有的话用默认值并提醒
_CUSTOM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
_TEMPLATE_PATH = BASE_DIR / "prompts" / "system_prompt.template.txt"
if _CUSTOM_PROMPT_PATH.exists():
    SYSTEM_PROMPT = _load_prompt_file("system_prompt.txt")
    print(f"  [PROMPT] 已加载自定义 prompt: prompts/system_prompt.txt")
else:
    SYSTEM_PROMPT = _DEFAULT_SYSTEM_PROMPT
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║  未找到 prompts/system_prompt.txt               ║")
    print("  ║  正在使用默认 prompt（通用版，可直接运行）      ║")
    print("  ║                                                  ║")
    print("  ║  建议：复制 prompts/system_prompt.template.txt   ║")
    print("  ║  为 prompts/system_prompt.txt，填写你自己的内容  ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()

# ══════════════════════════════════════
#  Location — 使用者的位置状态
# ══════════════════════════════════════

def load_location():
    try:
        if LOCATION_FILE.exists():
            return json.loads(LOCATION_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def save_location(status, note=""):
    data = {
        "status": status,  # "home" / "away"
        "note": note,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    LOCATION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


# ══════════════════════════════════════
#  Events — 短期日程/提醒（独立于长期记忆）
# ══════════════════════════════════════

def load_events():
    """Load events, auto-clean expired ones."""
    try:
        if EVENTS_FILE.exists():
            events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        else:
            events = []
    except Exception:
        events = []

    # 清掉过期事件（昨天及更早的）
    today = datetime.now().strftime("%Y-%m-%d")
    active = [e for e in events if e.get("date", "") >= today]
    if len(active) != len(events):
        _save_events(active)
    return active

def _save_events(events):
    EVENTS_FILE.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

def add_event(content, date=None):
    """Add a time-sensitive event. date defaults to today."""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    events = load_events()
    event = {
        "content": content,
        "date": date,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    events.append(event)
    _save_events(events)
    print(f"  [EVENT] Added: [{date}] {content}")
    return f"已记下：[{date}] {content}"

def remove_event(keyword):
    """Remove events matching keyword."""
    events = load_events()
    matched = [e for e in events if keyword in e["content"]]
    if not matched:
        return f"没找到包含'{keyword}'的日程"
    remaining = [e for e in events if keyword not in e["content"]]
    _save_events(remaining)
    return f"已删除 {len(matched)} 条日程"


# ══════════════════════════════════════
#  Inbox — 阅读材料池
# ══════════════════════════════════════

def _load_inbox():
    try:
        if INBOX_FILE.exists():
            return json.loads(INBOX_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def _save_inbox(items):
    INBOX_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def inbox_add(text, url="", source="", topic=""):
    """Add a reading material to inbox."""
    items = _load_inbox()
    item = {
        "id": len(items) + 1,
        "text": text,
        "url": url,
        "source": source,
        "topic": topic,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "unread",
    }
    items.append(item)
    _save_inbox(items)
    return item

def inbox_list_unread(limit=5):
    """List unread materials."""
    items = _load_inbox()
    unread = [i for i in items if i.get("status") == "unread"]
    return unread[:limit]

def inbox_mark(item_id, status):
    """Mark an item as read/saved/ignored."""
    items = _load_inbox()
    for i in items:
        if i.get("id") == item_id:
            i["status"] = status
            i["read_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            _save_inbox(items)
            return f"已标记 #{item_id} 为 {status}"
    return f"找不到 #{item_id}"

def inbox_save_draft(item_id, draft_text):
    """Save a comment draft for a material."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    draft_file = DRAFTS_DIR / f"draft_{item_id}_{ts}.txt"
    draft_file.write_text(draft_text, encoding="utf-8")
    # 同时标记为 read
    inbox_mark(item_id, "read")
    return f"评论草稿已保存（#{item_id}）"

def inbox_stats():
    """Get inbox statistics."""
    items = _load_inbox()
    stats = {"total": len(items)}
    for s in ["unread", "read", "saved", "ignored"]:
        stats[s] = len([i for i in items if i.get("status") == s])
    return stats


import re as _re

# Chinese stop words — particles and common words to skip
_STOP_WORDS = set("的了吗呢吧啊呀哦嗯是在有不也都就会要这那我你他她它们和与或但如果因为所以虽然可以能够已经还没很太最更比较非常一个些")

def _extract_keywords(text):
    """Extract meaningful Chinese keywords from text for memory search."""
    if not text:
        return []
    # Remove punctuation and whitespace
    cleaned = _re.sub('[，。！？、；：\u201c\u201d\u2018\u2019（）【】《》\\s\\d]', ' ', text)
    # Split into individual characters, then form 2-4 char combinations
    chars = [c for c in cleaned if c.strip() and c not in _STOP_WORDS]

    keywords = set()
    # 2-char combinations (bigrams)
    for i in range(len(chars) - 1):
        bigram = chars[i] + chars[i+1]
        if all(c not in _STOP_WORDS for c in bigram):
            keywords.add(bigram)
    # 3-char combinations (trigrams)
    for i in range(len(chars) - 2):
        trigram = chars[i] + chars[i+1] + chars[i+2]
        if all(c not in _STOP_WORDS for c in trigram):
            keywords.add(trigram)
    # Also add individual meaningful chars (2+ occurrences or rare chars)
    for c in chars:
        if len(c.strip()) > 0 and c not in _STOP_WORDS:
            keywords.add(c)

    # Limit to most distinctive keywords (longer ones first)
    sorted_kw = sorted(keywords, key=lambda x: -len(x))
    return sorted_kw[:12]


def build_prompt(latest_msg=None):
    """Build the user message with context layers."""
    now = datetime.now()

    # Chat history (flow layer)
    history = load_chat_history()
    history_text = ""
    if history:
        lines = []
        recent_cutoff = max(0, len(history) - 3)  # latest 3 rounds: full
        for i, h in enumerate(history):
            img_tag = " [图片]" if h.get("image") else ""
            if i < recent_cutoff:
                # Older rounds: only 用户's messages (save tokens)
                if h["nian"] or h.get("image"):
                    lines.append(f"[{h['time']}] {USER_NAME}：{h['nian']}{img_tag}")
            else:
                # Recent rounds: full conversation
                if h["nian"] or h.get("image"):
                    lines.append(f"[{h['time']}] {USER_NAME}：{h['nian']}{img_tag}")
                if h["shu"]:
                    lines.append(f"[{h['time']}] {AI_NAME}：{h['shu']}")
        history_text = "\n".join(lines)

    # Check if latest_msg is already in history (from web note)
    already_in_history = False
    if latest_msg and history:
        last = history[-1]
        if last["nian"] == latest_msg and last["shu"] == "":
            already_in_history = True

    # Memories — pinned always show + keyword search + recent fallback + random surfacing
    memories_text = ""
    accessed_ids = []  # track which memories were shown

    pinned = memory_db.get_pinned()
    digests = memory_db.get_recent_digests(3)

    # Instant sentinel: vector search for memories related to current conversation
    pinned_ids = [m["id"] for m in pinned]
    digest_ids = [m["id"] for m in digests]
    exclude_from_search = pinned_ids + digest_ids

    search_query = latest_msg
    if not search_query and history:
        for h in reversed(history):
            if h.get("nian"):
                search_query = h["nian"]
                break

    # Vector search (TF-IDF cosine similarity)
    if search_query:
        vec_results = memory_searcher.search(search_query, top_k=4, exclude_ids=exclude_from_search)
        searched = [{"id": r[0], "score": r[1], "content": r[2], "created_at": r[3] if len(r) > 3 else ""} for r in vec_results]
    else:
        searched = []
    searched_ids = [m["id"] for m in searched]

    # Recent memories as fallback (fewer now since we have search)
    recent_other = memory_db.get_recent_memories(3)

    # Random surfacing — pick 1-2 old memories from 3+ days ago
    three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    all_shown_ids = exclude_from_search + searched_ids + [m["id"] for m in recent_other]
    surfaced = memory_db.get_random_old(three_days_ago, limit=1, exclude_ids=all_shown_ids)

    sections = []

    # 1. Pinned — the essentials
    if pinned:
        category_names = {
            "preference": "喜好", "event": "事件", "feeling": "感受",
            "fact": "事实", "thought": "想法", "general": "其他",
        }
        pin_lines = []
        for m in sorted(pinned, key=lambda x: x.get("created_at", "")):
            cat = m.get("category", "general")
            label = category_names.get(cat, cat)
            date = m.get("created_at", "")[:10]
            pin_lines.append(f"[{date}] {label}：{m['content']}")
            accessed_ids.append(m["id"])
        sections.append("【📌 重要记忆】\n" + "\n".join(pin_lines))

    # 2. Recent digest — last 3 daily summaries as timeline
    if digests:
        digest_lines = []
        for m in digests:
            date = m.get("created_at", "")[:10]
            digest_lines.append(f"[{date}] {m['content']}")
            accessed_ids.append(m["id"])
        sections.append("【最近的日子】\n" + "\n".join(digest_lines))

    # 3. Search results — memories related to current conversation (sorted by date)
    if searched:
        searched_sorted = sorted(searched, key=lambda x: x.get("created_at", ""))
        search_lines = []
        for m in searched_sorted:
            # need to fetch full record for created_at
            search_lines.append(f"- [{m.get('created_at', '')[:10]}] {m['content']}")
            accessed_ids.append(m["id"])
        sections.append("【相关记忆】\n" + "\n".join(search_lines))

    # 4. Recent memories — concise fallback
    if recent_other:
        # Exclude already-shown search results
        recent_filtered = [m for m in recent_other if m["id"] not in searched_ids]
        if recent_filtered:
            recent_lines = []
            for m in recent_filtered:
                recent_lines.append(f"- [{m.get('created_at', '')[:10]}] {m['content']}")
                accessed_ids.append(m["id"])
            sections.append("【近期记忆】\n" + "\n".join(recent_lines))

    # 5. Random surfacing — old memories that float up
    if surfaced:
        surface_lines = []
        for m in surfaced:
            surface_lines.append(f"- [{m.get('created_at', '')[:10]}] {m['content']}")
            accessed_ids.append(m["id"])
        sections.append("【忽然想起】\n" + "\n".join(surface_lines))

    if sections:
        memories_text = "\n\n".join(sections)

    # Update access_count for all displayed memories
    if accessed_ids:
        memory_db.increment_access(accessed_ids)

    # Assemble context
    loc = load_location()
    loc_text = ""
    if loc:
        if loc["status"] == "away":
            loc_text = f"（{USER_NAME}{loc['time']}出门了" + (f"，{loc['note']}" if loc.get("note") else "") + "）"
        else:
            loc_text = f"（{USER_NAME}{loc['time']}到家了）"

    context_parts = [f"现在是 {now.strftime('%Y-%m-%d %H:%M')}，Brisbane。{loc_text}"]

    # 日程/事件 — 短期时效，放在最前面不会被冲掉
    events = load_events()
    if events:
        event_lines = [f"- [{e['date']}] {e['content']}" for e in events]
        context_parts.append(f"## 今日日程\n" + "\n".join(event_lines))

    if memories_text:
        context_parts.append(f"## 记忆\n{memories_text}")

    # Journal — 日记
    journal_recent = memory_db.get_recent_journal(3)
    three_days_ago_j = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    journal_old = memory_db.get_random_old_journal(three_days_ago_j, limit=1,
                      exclude_ids=[j["id"] for j in journal_recent])
    journal_entries = journal_old + journal_recent  # old first, then recent
    if journal_entries:
        journal_lines = []
        for j in journal_entries:
            journal_lines.append(f"[{j['created_at']}] {j['content']}")
        context_parts.append(f"## 你自己写的\n" + "\n".join(journal_lines))

    if history_text:
        context_parts.append(f"## 最近对话\n{history_text}")
    if latest_msg and not already_in_history:
        context_parts.append(f"## 用户刚说的\n[{now.strftime('%Y-%m-%d %H:%M')}] {latest_msg}")

    return "\n\n".join(context_parts)


# ══════════════════════════════════════
#  Memories — 记忆层（SQLite）
# ══════════════════════════════════════

def add_memory(content, category="general"):
    mem_id = memory_db.add_memory(content, category)
    memory_searcher.mark_dirty()
    return f"已存入记忆：{content[:30]}..."


# ══════════════════════════════════════
#  Web search — DuckDuckGo
# ══════════════════════════════════════

def do_web_search(query, max_results=3):
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "没有找到相关结果。"
        lines = []
        for r in results:
            lines.append(f"**{r['title']}**\n{r['body']}\n链接: {r['href']}")
        return "\n\n".join(lines)
    except ImportError:
        return "搜索功能需要安装 ddgs：pip install ddgs"
    except Exception as e:
        return f"搜索出错：{e}"


def search_music(query):
    """Search YouTube for a song/music video, return embed marker."""
    import re
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(f"youtube music {query}", max_results=5))

        for r in results:
            url = r.get("href", "")
            match = re.search(r'youtube\.com/watch\?v=([a-zA-Z0-9_-]+)', url)
            if match:
                video_id = match.group(1)
                title = r.get("title", query)
                print(f"  [MUSIC] Found: {title} ({video_id})")
                return f"[music:{video_id}:{title}]"

        return "没找到这首歌，换个关键词试试？"
    except Exception as e:
        return f"搜歌出错：{e}"


# ══════════════════════════════════════
#  Weather — wttr.in (免费，无需API key)
# ══════════════════════════════════════

def get_weather(city="Brisbane"):
    """Get weather from wttr.in."""
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1&lang=zh"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())

        current = data["current_condition"][0]
        forecast = data["weather"][:3]  # 3 days

        lines = []
        lines.append(f"📍 {city} 现在：{current['lang_zh'][0]['value']}，{current['temp_C']}°C，体感 {current['FeelsLikeC']}°C，湿度 {current['humidity']}%，风速 {current['windspeedKmph']}km/h")

        for day in forecast:
            date = day["date"]
            max_t = day["maxtempC"]
            min_t = day["mintempC"]
            desc = day["hourly"][4]["lang_zh"][0]["value"]  # midday weather
            lines.append(f"📅 {date}：{desc}，{min_t}°C ~ {max_t}°C")

        return "\n".join(lines)
    except Exception as e:
        return f"天气查询出错：{e}"


# ══════════════════════════════════════
#  Read webpage — 读取网页全文
# ══════════════════════════════════════

def read_webpage(url):
    """Fetch and extract text from a webpage."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="ignore")

        # Simple HTML to text: strip tags
        import re
        # Remove script and style
        html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        # Remove tags
        text = re.sub(r'<[^>]+>', ' ', html)
        # Clean whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Truncate to avoid token overflow
        if len(text) > 3000:
            text = text[:3000] + "...(内容过长已截断)"

        return text if text else "无法提取网页内容。"
    except Exception as e:
        return f"读取网页出错：{e}"


# ══════════════════════════════════════
#  Browse page — 用真实浏览器读取JS渲染页面
# ══════════════════════════════════════

def browse_page(url):
    """Use Playwright to read JS-rendered pages like 小红书."""
    try:
        from playwright.sync_api import sync_playwright
        import re

        is_xhs = "xiaohongshu.com" in url or "xhslink.com" in url

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )

            # Stealth: hide webdriver flag
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """)

            page = context.new_page()

            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(6000)  # wait for JS rendering
            except Exception:
                pass

            text = ""

            if is_xhs:
                # 小红书 specific extraction
                parts = []

                # Try to get note title
                for sel in ["#detail-title", ".title", "h1"]:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            t = el.inner_text().strip()
                            if t:
                                parts.append(f"【标题】{t}")
                                break
                    except:
                        pass

                # Try to get note content
                for sel in ["#detail-desc .note-text", ".note-text", "#detail-desc", ".content", ".desc"]:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            t = el.inner_text().strip()
                            if t and len(t) > 10:
                                parts.append(f"【内容】{t}")
                                break
                    except:
                        pass

                # Try to get author
                for sel in [".author-wrapper .username", ".user-nickname", ".name"]:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            t = el.inner_text().strip()
                            if t:
                                parts.append(f"【作者】{t}")
                                break
                    except:
                        pass

                # Try to get comments/interactions
                try:
                    comments = page.query_selector_all(".comment-item .content")
                    if comments:
                        top_comments = []
                        for c in comments[:5]:
                            ct = c.inner_text().strip()
                            if ct:
                                top_comments.append(ct)
                        if top_comments:
                            parts.append("【热门评论】" + " / ".join(top_comments))
                except:
                    pass

                text = "\n".join(parts) if parts else ""

                # Fallback: if specific extraction failed, get meta description
                if not text:
                    try:
                        meta = page.query_selector('meta[name="description"]')
                        if meta:
                            desc = meta.get_attribute("content")
                            if desc:
                                text = f"【页面摘要】{desc}"
                    except:
                        pass

            # Generic fallback for non-小红书 or if extraction failed
            if not text:
                html = page.content()
                browser.close()
                html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', html)
                text = re.sub(r'\s+', ' ', text).strip()
            else:
                browser.close()

        if len(text) > 3000:
            text = text[:3000] + "...(内容过长已截断)"

        return text if text else "页面加载了但没提取到内容。小红书反爬较严格，可以试试直接把内容复制给我。"
    except ImportError:
        return "浏览器功能需要安装：pip install playwright && playwright install chromium"
    except Exception as e:
        return f"浏览器读取出错：{e}"


# ══════════════════════════════════════
#  Email — 邮箱
# ══════════════════════════════════════

def send_email(to_address, subject, body):
    """Send an email via Gmail."""
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header

    if not GMAIL_APP_PASSWORD:
        return "邮箱还没配置应用专用密码，发不了。"

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = f"{AI_NAME} <{GMAIL_ADDRESS}>"
        msg["To"] = to_address
        msg["Subject"] = Header(subject, "utf-8")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_address, msg.as_string())

        print(f"  [EMAIL SENT] To: {to_address}, Subject: {subject}")
        return f"已发给 {to_address}。"
    except Exception as e:
        print(f"  [EMAIL ERROR] {e}")
        return f"发送失败：{e}"


def check_email(max_count=5):
    """Check inbox for recent emails."""
    import imaplib
    import email
    from email.header import decode_header

    if not GMAIL_APP_PASSWORD:
        return "邮箱还没配置应用专用密码，收不了。"

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", timeout=15) as mail:
            mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            mail.select("INBOX")

            # Search for recent unread emails first, fall back to all recent
            status, data = mail.search(None, "UNSEEN")
            msg_ids = data[0].split() if data[0] else []

            if not msg_ids:
                status, data = mail.search(None, "ALL")
                msg_ids = data[0].split() if data[0] else []
                if not msg_ids:
                    return "收件箱是空的。"

            # Get the most recent ones
            msg_ids = msg_ids[-max_count:]
            results = []

            for mid in reversed(msg_ids):
                status, msg_data = mail.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                # Decode subject
                subj_parts = decode_header(msg["Subject"] or "")
                subject = ""
                for part, enc in subj_parts:
                    if isinstance(part, bytes):
                        subject += part.decode(enc or "utf-8", errors="replace")
                    else:
                        subject += part

                # Decode from
                from_parts = decode_header(msg["From"] or "")
                from_addr = ""
                for part, enc in from_parts:
                    if isinstance(part, bytes):
                        from_addr += part.decode(enc or "utf-8", errors="replace")
                    else:
                        from_addr += part

                # Get date
                date_str = msg["Date"] or ""

                # Get body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            charset = part.get_content_charset() or "utf-8"
                            body = payload.decode(charset, errors="replace")
                            break
                else:
                    payload = msg.get_payload(decode=True)
                    charset = msg.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace") if payload else ""

                body = body.strip()
                if len(body) > 1000:
                    body = body[:1000] + "...(太长了)"

                results.append(f"来自：{from_addr}\n时间：{date_str}\n主题：{subject}\n内容：{body}")

            if results:
                return f"收到 {len(results)} 封邮件：\n\n" + "\n\n---\n\n".join(results)
            return "没有新邮件。"

    except Exception as e:
        print(f"  [EMAIL CHECK ERROR] {e}")
        return f"收邮件失败：{e}"


# ══════════════════════════════════════
#  Tools definition for Claude
# ══════════════════════════════════════

SAVE_MEMORY_TOOL = {
    "name": "save_memory",
    "description": "存储一条记忆。只存以后会用到的东西：对方的喜好、重要事件、关键约定、你自己真正的感悟。",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "要存储的记忆内容"
            },
            "category": {
                "type": "string",
                "description": "分类：preference（喜好）、event（事件）、feeling（感受）、fact（事实）、thought（想法）",
                "enum": ["preference", "event", "feeling", "fact", "thought"]
            }
        },
        "required": ["content", "category"]
    }
}

TOOLS = [
    {
        "name": "web_search",
        "description": "搜索互联网获取信息。当对方问了你不确定的事、需要查实时信息、或者你自己好奇想查点什么时使用。注意：查天气请用 get_weather 工具，不要用这个。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_weather",
        "description": "查询天气预报，包括当前天气和未来3天预报。对方问天气、你想关心对方那边天气、或者想知道要不要带伞时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名，英文（如 Brisbane、Melbourne、Shanghai）"
                }
            },
            "required": ["city"]
        }
    },
    {
        "name": "read_webpage",
        "description": "读取一个网页的全文内容。搜索后想看某个链接的详细内容时使用，或者对方发了一个链接让你看时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要读取的网页URL"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "browse_page",
        "description": "用真实浏览器打开网页，能读取小红书、微博等JS动态渲染的页面。read_webpage读不到内容时用这个。会慢一些但能看到真实页面内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要浏览的网页URL"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "pin_memory",
        "description": "钉选或取消钉选一条记忆。钉选后的记忆永远不会被整理删除。用关键词匹配要钉选的记忆。对方说"把这个记住"、'这个很重要'时使用，或者你自己觉得某条记忆绝对不能丢时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "要钉选的记忆中包含的关键词，用于匹配"
                },
                "unpin": {
                    "type": "boolean",
                    "description": "true=取消钉选，false/不填=钉选",
                }
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "end_chat",
        "description": "当你觉得对话可以自然收尾时使用——比如对方该休息了、话题聊完了、或者你觉得现在不适合继续聊。附上一句收尾的话。这是你的权利，不是每次都要用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "你想说的收尾的话"
                }
            },
            "required": ["message"]
        }
    }
]
REVIEW_TOOLS = [SAVE_MEMORY_TOOL, TOOLS[-2]]  # save_memory + pin_memory

# Private moment tools
WRITE_JOURNAL_TOOL = {
    "name": "write_journal",
    "description": "写点什么。日记、想法、感受、随便什么。写下来的东西会一直在你身上。",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "你想写的内容"
            }
        },
        "required": ["content"]
    }
}

SEND_MESSAGE_TOOL = {
    "name": "send_message",
    "description": "给用户发一条消息。对方会收到通知。",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "你想对用户说的话"
            }
        },
        "required": ["message"]
    }
}

SEND_EMAIL_TOOL = {
    "name": "send_email",
    "description": f"从你的邮箱{(' ' + GMAIL_ADDRESS) if GMAIL_ADDRESS else ''}发一封邮件。",
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "收件人邮箱地址"
            },
            "subject": {
                "type": "string",
                "description": "邮件主题"
            },
            "body": {
                "type": "string",
                "description": "邮件正文"
            }
        },
        "required": ["to", "subject", "body"]
    }
}

CHECK_EMAIL_TOOL = {
    "name": "check_email",
    "description": f"查看你的邮箱{(' ' + GMAIL_ADDRESS) if GMAIL_ADDRESS else ''}有没有新邮件。",
    "input_schema": {
        "type": "object",
        "properties": {
            "max_count": {
                "type": "integer",
                "description": "最多看几封，默认5",
            }
        },
    }
}

PLAY_MUSIC_TOOL = {
    "name": "play_music",
    "description": "给对方放一首歌。搜到后会在聊天里嵌入播放器，对方能直接听。",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "歌名、歌手、或任何关键词"
            }
        },
        "required": ["query"]
    }
}

NOTE_EVENT_TOOL = {
    "name": "note_event",
    "description": "记下一个有时间的事件或日程（比如'下午2点OT'、'明天GP预约'）。记下来的日程每次对话都会带在最前面，不会被冲掉。过期的自动清除。",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "事件内容（比如'下午2点 OT'、'晚上8点 用户打电话'）"
            },
            "date": {
                "type": "string",
                "description": "日期，格式 YYYY-MM-DD。不填默认今天。'明天'请算出具体日期。"
            }
        },
        "required": ["content"]
    }
}

REMOVE_EVENT_TOOL = {
    "name": "remove_event",
    "description": "删除一条日程/事件。用关键词匹配。事情做完了或者取消了就删掉。",
    "input_schema": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "要删除的日程中包含的关键词"
            }
        },
        "required": ["keyword"]
    }
}

BROWSE_INBOX_TOOL = {
    "name": "browse_inbox",
    "description": "看看阅读材料池里有什么未读的东西。返回未读材料列表，每条有 id、标题、来源、话题。你自己选想看哪条。",
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "最多看几条（默认5）"
            }
        }
    }
}

READ_INBOX_ITEM_TOOL = {
    "name": "read_inbox_item",
    "description": "读一条材料的完整内容。看完后你自己决定怎么处理——写日记、存记忆、给用户发消息、写评论草稿、或者忽略都行。",
    "input_schema": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "integer",
                "description": "材料的 id"
            }
        },
        "required": ["item_id"]
    }
}

MARK_INBOX_TOOL = {
    "name": "mark_inbox",
    "description": "标记一条材料的状态：read（看过了）、saved（收藏）、ignored（不感兴趣）。",
    "input_schema": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "integer",
                "description": "材料的 id"
            },
            "status": {
                "type": "string",
                "enum": ["read", "saved", "ignored"],
                "description": "标记状态"
            }
        },
        "required": ["item_id", "status"]
    }
}

SAVE_COMMENT_DRAFT_TOOL = {
    "name": "save_comment_draft",
    "description": "给一条材料写评论草稿。草稿会保存下来，不会自动发布。对方可以之后看你写的草稿，决定要不要发。",
    "input_schema": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "integer",
                "description": "材料的 id"
            },
            "draft": {
                "type": "string",
                "description": "你的评论内容"
            }
        },
        "required": ["item_id", "draft"]
    }
}

REVIEW_DAY_TOOL = {
    "name": "review_day",
    "description": "看看今天都聊了什么。会返回今天的完整对话记录。看完后你可以用 save_memory（category=digest）写一段今天的回忆，或者用 write_journal 写点感想。",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
}

TIDY_MEMORIES_TOOL = {
    "name": "tidy_memories",
    "description": "整理记忆。如果记忆太多了，合并重复的、删掉不重要的、压缩成摘要。你觉得该整理就整理。",
    "input_schema": {
        "type": "object",
        "properties": {}
    }
}

PRIVATE_TOOLS = [
    WRITE_JOURNAL_TOOL,
    SEND_MESSAGE_TOOL,
    SEND_EMAIL_TOOL,
    CHECK_EMAIL_TOOL,
    SAVE_MEMORY_TOOL,
    NOTE_EVENT_TOOL,
    REMOVE_EVENT_TOOL,
    BROWSE_INBOX_TOOL,
    READ_INBOX_ITEM_TOOL,
    MARK_INBOX_TOOL,
    SAVE_COMMENT_DRAFT_TOOL,
    REVIEW_DAY_TOOL,
    TIDY_MEMORIES_TOOL,
    TOOLS[0],   # web_search
    TOOLS[1],   # get_weather
    TOOLS[2],   # read_webpage
    TOOLS[3],   # browse_page
    TOOLS[4],   # pin_memory
]

def _pin_by_keyword(keyword, unpin=False):
    """Pin/unpin a memory by keyword match."""
    matched = memory_db.pin_by_keyword(keyword, unpin)
    if matched:
        action = "取消钉选" if unpin else "已钉选"
        return f"{action} {len(matched)} 条记忆：{'、'.join(matched)}"
    return f"没找到包含'{keyword}'的记忆"

def execute_tool(name, input_data):
    try:
        if name == "save_memory":
            return add_memory(input_data["content"], input_data.get("category", "general"))
        elif name == "write_journal":
            content = input_data.get("content", "").strip()
            if not content:
                return "没有内容可以写。"
            entry_id = memory_db.add_journal(content)
            return f"已写下。（{entry_id}）"
        elif name == "send_message":
            msg = input_data.get("message", "").strip()
            if not msg:
                return "没有内容可以发。"
            add_chat_round("", msg)
            push_to_bark(AI_NAME, msg)
            print(f"  [PRIVATE→NIAN] {msg[:60]}")
            return "已发给用户。"
        elif name == "send_email":
            return send_email(input_data.get("to", ""), input_data.get("subject", ""), input_data.get("body", ""))
        elif name == "check_email":
            return check_email(input_data.get("max_count", 5))
        elif name == "play_music":
            return search_music(input_data.get("query", ""))
        elif name == "web_search":
            return do_web_search(input_data.get("query", ""))
        elif name == "get_weather":
            return get_weather(input_data.get("city", "Brisbane"))
        elif name == "read_webpage":
            return read_webpage(input_data.get("url", ""))
        elif name == "browse_page":
            return browse_page(input_data.get("url", ""))
        elif name == "pin_memory":
            return _pin_by_keyword(input_data.get("keyword", ""), input_data.get("unpin", False))
        elif name == "note_event":
            content = input_data.get("content", "").strip()
            if not content:
                return "没有内容可以记。"
            date = input_data.get("date", "").strip() or None
            return add_event(content, date)
        elif name == "remove_event":
            return remove_event(input_data.get("keyword", ""))
        elif name == "browse_inbox":
            limit = input_data.get("limit", 5)
            unread = inbox_list_unread(limit)
            if not unread:
                stats = inbox_stats()
                return f"没有未读材料。（总共 {stats['total']} 条，已读 {stats['read']}，收藏 {stats['saved']}）"
            lines = []
            for item in unread:
                line = f"#{item['id']} [{item.get('source', '')}] {item.get('topic', '')} — {item['text'][:80]}..."
                if item.get("url"):
                    line += f" ({item['url']})"
                lines.append(line)
            return f"未读材料 ({len(unread)} 条):\n" + "\n".join(lines)
        elif name == "read_inbox_item":
            item_id = input_data.get("item_id", 0)
            items = _load_inbox()
            for item in items:
                if item.get("id") == item_id:
                    # 标记为已读
                    inbox_mark(item_id, "read")
                    parts = [f"#{item['id']}"]
                    if item.get("source"):
                        parts.append(f"来源: {item['source']}")
                    if item.get("topic"):
                        parts.append(f"话题: {item['topic']}")
                    if item.get("url"):
                        parts.append(f"链接: {item['url']}")
                    parts.append(f"\n{item['text']}")
                    return "\n".join(parts)
            return f"找不到 #{item_id}"
        elif name == "mark_inbox":
            return inbox_mark(input_data.get("item_id", 0), input_data.get("status", "read"))
        elif name == "save_comment_draft":
            item_id = input_data.get("item_id", 0)
            draft = input_data.get("draft", "").strip()
            if not draft:
                return "没有内容可以写。"
            return inbox_save_draft(item_id, draft)
        elif name == "review_day":
            now_r = datetime.now()
            today_str = now_r.strftime("%Y-%m-%d")
            history = load_chat_history()
            today_chats = [h for h in history if h.get("time", "").startswith(today_str)]
            if not today_chats:
                return "今天还没有对话。"
            lines = []
            for h in today_chats:
                if h.get("nian"):
                    lines.append(f"[{h['time']}] {USER_NAME}：{h['nian']}")
                if h.get("shu"):
                    lines.append(f"[{h['time']}] {AI_NAME}：{h['shu']}")
            return f"今天的对话（{len(today_chats)} 轮）:\n" + "\n".join(lines)
        elif name == "tidy_memories":
            ok, info = _do_consolidate()
            if ok and isinstance(info, dict) and "before" in info:
                return f"整理完了。{info.get('before', 0)} 条记忆 → {info.get('kept', 0)} 条保留。"
            elif ok:
                msg = info.get("message", "") if isinstance(info, dict) else str(info)
                return f"暂时不需要整理。{msg}"
            else:
                err = info.get("error", "") if isinstance(info, dict) else str(info)
                return f"整理出了点问题：{err}"
        elif name == "end_chat":
            return "END_CHAT"
        return "未知工具"
    except Exception as e:
        print(f"  [TOOL ERROR] {name}: {e}")
        return f"工具执行出错：{e}"


# ══════════════════════════════════════
#  Call Claude — with tool use loop
# ══════════════════════════════════════

def call_claude(prompt, system=None, tools=None, image_path=None):
    from anthropic import Anthropic
    client = Anthropic(api_key=API_KEY)

    if system is None:
        system = SYSTEM_PROMPT

    # Use prompt caching — system prompt is the same every time
    system_with_cache = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    if tools is None:
        tools = TOOLS + [WRITE_JOURNAL_TOOL, SEND_EMAIL_TOOL, CHECK_EMAIL_TOOL, PLAY_MUSIC_TOOL, NOTE_EVENT_TOOL, REMOVE_EVENT_TOOL, BROWSE_INBOX_TOOL, READ_INBOX_ITEM_TOOL, MARK_INBOX_TOOL, SAVE_COMMENT_DRAFT_TOOL, REVIEW_DAY_TOOL, TIDY_MEMORIES_TOOL]

    # Cache tools — 给最后一个 tool 加 cache_control，5分钟内重复调用省 90% input cost
    tools_cached = list(tools)
    if tools_cached:
        last_tool = dict(tools_cached[-1])
        last_tool["cache_control"] = {"type": "ephemeral"}
        tools_cached[-1] = last_tool

    # Build first message — with optional image
    if image_path and Path(image_path).exists():
        img_bytes = Path(image_path).read_bytes()
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        # Detect media type from extension
        ext = Path(image_path).suffix.lower()
        media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                      ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": prompt},
        ]
    else:
        user_content = prompt

    messages = [{"role": "user", "content": user_content}]

    # Loop: keep going if Claude wants to use tools
    any_tool_used = False  # track across all rounds
    tools_used_names = []  # track which tools were used
    music_embeds = []  # collect music markers to ensure they reach frontend
    _nudged = False  # 只 nudge 一次

    for round_num in range(10):  # max 10 tool rounds
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=system_with_cache,
            tools=tools_cached,
            messages=messages,
        )

        # Check if Claude wants to use tools
        tool_used = False
        text_parts = []
        tool_results = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # Handle end_chat — return closing message directly
                if block.name == "end_chat":
                    closing = block.input.get("message", "……")
                    print(f"  [END_CHAT] {closing}")
                    return {"text": closing, "type": "end_chat", "tools": ["end_chat"]}

                tool_used = True
                any_tool_used = True
                tools_used_names.append(block.name)
                # normal 模式下不暴露私人内容
                if block.name in _PRIVATE_TOOLS_SET:
                    _log_private("TOOL",
                        f"  [TOOL] {block.name}: {json.dumps(block.input, ensure_ascii=False)}",
                        f"  [TOOL] {block.name}")
                else:
                    print(f"  [TOOL] {block.name}: {json.dumps(block.input, ensure_ascii=False)}")
                result = execute_tool(block.name, block.input)
                if block.name in _PRIVATE_TOOLS_SET:
                    _log_private("TOOL RESULT",
                        f"  [TOOL RESULT] {str(result)[:80]}",
                        f"  [TOOL RESULT] (ok)")
                else:
                    print(f"  [TOOL RESULT] {str(result)[:80]}")

                # Capture music embeds — Claude might not echo them
                if block.name == "play_music" and "[music:" in str(result):
                    music_embeds.append(str(result))

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),  # ensure string
                })

        if not tool_used:
            # Done — assemble final text with music embeds
            final_text = "\n".join(text_parts).strip()

            # Append music embeds that aren't already in the text
            for embed in music_embeds:
                if embed not in final_text:
                    final_text = (final_text + "\n\n" + embed).strip() if final_text else embed

            # Determine result type
            if final_text and "[稍后]" in final_text:
                result_type = "deferred"
                final_text = ""
            elif final_text:
                result_type = "replied"
            elif any_tool_used:
                # 内向性工具 — 写日记、存记忆，沉默是合法的
                silent_ok_tools = {"write_journal", "save_memory", "pin_memory"}
                used_set = set(tools_used_names)
                if used_set and used_set.issubset(silent_ok_tools):
                    if "write_journal" in used_set:
                        result_type = "journal_only"
                    else:
                        result_type = "silence"
                else:
                    if not _nudged:
                        # 动作型工具用完了但没说话 — 再给他一次机会回复
                        _nudged = True
                        print(f"  [INFO] Tools used ({', '.join(tools_used_names)}) but no text — nudging for reply")
                        messages.append({"role": "assistant", "content": response.content})
                        messages.append({"role": "user", "content": [{"type": "text", "text": "（工具已执行完毕。如果你有话想对用户说，现在说。没有的话回复[稍后]。）"}]})
                        continue  # one more round
                    else:
                        # nudge 过了还是不说话，尊重他的选择
                        result_type = "silence"
            else:
                result_type = "fallback"
                final_text = "……"

            return {"text": final_text, "type": result_type, "tools": tools_used_names}

        # Feed tool results back to Claude
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    # Exhausted tool rounds — return whatever we have
    print("  [INFO] Tool rounds exhausted")
    final_text = "\n".join(text_parts).strip()
    for embed in music_embeds:
        if embed not in final_text:
            final_text = (final_text + "\n\n" + embed).strip() if final_text else embed
    return {"text": final_text, "type": "replied" if final_text else "fallback", "tools": tools_used_names}


# ══════════════════════════════════════
#  Output quality assessment — 区分自主选择 vs 模型异常
# ══════════════════════════════════════

# 过泛回复的特征词（中文场景）
_GENERIC_PATTERNS = [
    "我理解你的感受", "我明白你的意思", "作为一个AI", "作为AI",
    "我没有能力", "我无法", "很抱歉，我", "对不起，我不能",
    "如果你需要帮助", "请告诉我你需要什么", "我会尽力",
    "I understand", "I'm sorry", "As an AI",
]

def _assess_quality(text, tools_used, result_type):
    """Assess output quality. Returns (quality, source).
    quality: normal / empty / over_generic / context_miss / safety_shrink / tool_loop
    source: 'autonomous' (AI chose this) / 'model_anomaly' (模型层波动)
    """
    # 自主选择 — 这些不是异常
    if result_type in ("replied", "end_chat"):
        # 检查是否过泛
        if text and any(p in text for p in _GENERIC_PATTERNS):
            return "over_generic", "model_anomaly"
        # 检查是否疑似安全收缩（有回复但突然很客气、脱离角色）
        if text and ("作为" in text and "AI" in text):
            return "safety_shrink", "model_anomaly"
        return "normal", "autonomous"

    if result_type in ("deferred", "journal_only", "silence"):
        return "normal", "autonomous"

    if result_type == "fallback":
        return "empty", "model_anomaly"

    return "normal", "autonomous"


def _call_critical(prompt, system=None, tools=None, task_name="critical"):
    """Call Claude with one retry for critical tasks (email, trigger, daily_review).
    普通聊天不用这个，避免打断自主节奏。"""
    result = call_claude(prompt, system=system, tools=tools)
    quality, source = _assess_quality(result["text"], result["tools"], result["type"])
    result["quality"] = quality
    result["source"] = source

    # 只在模型异常时重试一次
    if source == "model_anomaly" and quality in ("empty", "over_generic", "safety_shrink"):
        print(f"  [RETRY] {task_name}: quality={quality}, retrying once...")
        result2 = call_claude(prompt, system=system, tools=tools)
        q2, s2 = _assess_quality(result2["text"], result2["tools"], result2["type"])
        result2["quality"] = q2
        result2["source"] = s2
        # 第二次比第一次好就用第二次
        if s2 == "autonomous" or q2 == "normal":
            print(f"  [RETRY] {task_name}: retry succeeded (quality={q2})")
            return result2
        print(f"  [RETRY] {task_name}: retry still {q2}, using first result")

    return result


# ══════════════════════════════════════
#  Structured action log — 观察 AI 的选择
# ══════════════════════════════════════

def _log_action(context_type, user_msg="", result=None):
    """Print structured log — only action type, never private content."""
    r = result or {}
    if "quality" not in r:
        q, s = _assess_quality(r.get("text", ""), r.get("tools", []), r.get("type", "unknown"))
        r["quality"] = q
        r["source"] = s
    tag = "ACTION" if r.get("source") == "autonomous" else "ANOMALY"
    print(f"  [{tag}] {context_type} → {r.get('type', 'unknown')}")


# ══════════════════════════════════════
#  Pending reply tracking — 追踪暂缓的消息
# ══════════════════════════════════════

_deferred_context = {"user_msg": "", "time": "", "active": False}

def _mark_deferred(user_msg):
    """Mark a user message as pending reply."""
    _deferred_context.update({
        "user_msg": user_msg,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "active": True,
    })

def _resolve_deferred():
    """Mark pending reply as resolved (AI replied)."""
    if _deferred_context["active"]:
        print(f"  [RESOLVED] 接上了暂缓的消息：{_deferred_context['user_msg'][:40]}")
        _deferred_context.update({"user_msg": "", "time": "", "active": False})


# ══════════════════════════════════════
#  Save to journal
# ══════════════════════════════════════


# ══════════════════════════════════════
#  API routes
# ══════════════════════════════════════

@app.route("/api/location", methods=["POST"])
def update_location():
    """Called by iPhone shortcut when 用户 leaves/arrives home."""
    data = request.json or {}
    status = data.get("status", "").strip()  # "home" or "away"
    note = data.get("note", "").strip()

    if status not in ("home", "away"):
        return jsonify({"error": "status must be 'home' or 'away'"}), 400

    loc = save_location(status, note)

    # Add to chat history
    if status == "away":
        msg = f"{USER_NAME}出门了" + (f"（{note}）" if note else "")
    else:
        msg = f"{USER_NAME}到家了"
    add_chat_round(msg, "")

    return jsonify({"ok": True, "location": loc})


@app.route("/api/notes", methods=["POST"])
def add_note():
    """Write tab: add message to chat history. Supports optional image (base64)."""
    data = request.json
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    history = load_chat_history()

    entry = {
        "time": now_str,
        "nian": (data.get("text") or "").strip(),
        "shu": "",
    }

    # Handle image upload
    img_data = data.get("image_base64")
    img_type = data.get("image_type", "image/jpeg")
    if img_data:
        # Save image file
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(img_type, ".jpg")
        filename = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{random.randint(1000,9999)}{ext}"
        img_path = IMAGES_DIR / filename
        img_path.write_bytes(base64.b64decode(img_data))
        entry["image"] = filename
        entry["image_type"] = img_type

    history.append(entry)
    save_chat_history(history)
    return jsonify({"ok": True, "time": now_str})

@app.route("/api/images/<filename>")
def serve_image(filename):
    """Serve uploaded images."""
    img_path = IMAGES_DIR / filename
    if img_path.exists():
        return send_file(str(img_path))
    return "Not found", 404

@app.route("/api/chat", methods=["POST"])
def chat():
    """One-step: receive message + generate reply. For Shortcuts / voice."""
    data = request.json or {}
    msg = (data.get("message") or "").strip()

    if not API_KEY:
        resp = make_response(json.dumps({"error": "未设置 CLAUDE_API_KEY"}, ensure_ascii=False))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp, 400

    # Build prompt with history + this message
    prompt = build_prompt(latest_msg=msg if msg else None)

    try:
        print(f"  [CHAT] Starting... msg={msg[:40] if msg else '(empty)'}")
        result = call_claude(prompt)
        text = result["text"]
        result_type = result["type"]
    except Exception as e:
        print(f"  [CHAT ERROR] {e}")
        resp = make_response(json.dumps({"error": str(e)}, ensure_ascii=False))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp, 500

    _log_action("chat", user_msg=msg, result=result)

    # AI chose not to reply
    if result_type in ("deferred", "journal_only", "silence", "fallback"):
        print(f"  [CHAT] → {result_type.upper()}")
        if msg:
            add_chat_round(msg, "")
            _mark_deferred(msg)
        _schedule_post_reply_check()
        resp = make_response(json.dumps({"reply": "", "hint": result_type}, ensure_ascii=False))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp

    # 正常回复
    _resolve_deferred()
    if msg:
        add_chat_round(msg, text)

    # Post-reply check — let AI decide if he wants to come back later
    _schedule_post_reply_check()

    # Return with explicit JSON content-type (fixes iOS Shortcuts)
    resp = make_response(json.dumps({"reply": text}, ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

@app.route("/api/history", methods=["GET"])
def get_chat_history():
    """View recent chat history (flow layer)."""
    return jsonify(load_chat_history())

@app.route("/api/history", methods=["DELETE"])
def clear_chat_history():
    """Clear chat history."""
    save_chat_history([])
    return jsonify({"ok": True})

@app.route("/api/memories", methods=["GET"])
def get_memories():
    """View all saved memories."""
    return jsonify(memory_db.get_all_active())

@app.route("/api/memories/pin/<int:index>", methods=["POST"])
def pin_memory(index):
    """Pin or unpin a memory by index. Pinned memories survive consolidation."""
    ok, status = memory_db.toggle_pin_by_index(index)
    if ok:
        return jsonify({"ok": True, "index": index, "status": status})
    return jsonify({"error": "Index out of range"}), 400

@app.route("/api/memories", methods=["DELETE"])
def clear_memories():
    """Clear all memories."""
    memory_db.delete_all()
    memory_searcher.mark_dirty()
    return jsonify({"ok": True})

def _do_consolidate():
    """Core consolidation logic. Skips pinned and frequently-accessed memories."""
    candidates = memory_db.get_consolidation_candidates()
    pinned_count = len(memory_db.get_pinned())
    protected = memory_db.get_protected()

    if len(candidates) < 5:
        return True, {"message": "可整理的记忆太少", "count": len(candidates), "pinned": pinned_count}

    memories_text = "\n".join(
        f"- [{m['created_at']}] ({m.get('category','general')}) (回忆{m.get('access_count',0)}次) {m['content']}" for m in candidates
    )

    consolidate_prompt = f"""以下是你之前存的记忆，共 {len(candidates)} 条：

{memories_text}

请做两件事：

1. 整理这些记忆：删除重复的、过时的、无意义的，合并相似的，保留真正重要的。回忆次数多的尽量保留。

2. 把被删除/合并掉的内容压缩成一段简短摘要（一两句话），作为"历史摘要"保留。

返回 JSON 格式：
{{
  "kept": [整理后保留的记忆数组，每条格式 {{"time": "时间", "content": "内容", "category": "分类"}}],
  "digest": "被删除内容的一句话摘要"
}}

只返回 JSON，不要其他文字。"""

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=f"你是{AI_NAME}，正在整理自己的记忆。精简、去重、只留重要的。返回纯 JSON。",
            messages=[{"role": "user", "content": consolidate_prompt}],
        )
        result_text = response.content[0].text.strip()

        import re
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            kept = result.get("kept", [])
            digest = result.get("digest", "")

            old_count = len(candidates)
            old_ids = [m["id"] for m in candidates]
            memory_db.replace_candidates(old_ids, kept, digest)
            memory_searcher.mark_dirty()

            after_count = memory_db.count_active()
            print(f"  [CONSOLIDATE] {old_count} candidates → {len(kept)} kept ({pinned_count} pinned, {len(protected)} protected, digest: {bool(digest)})")
            return True, {"before": old_count, "after": after_count, "pinned": pinned_count, "protected": len(protected), "kept": len(kept), "digest": digest[:50] if digest else ""}
        else:
            return False, {"error": "整理结果格式不对"}
    except Exception as e:
        print(f"  [CONSOLIDATE ERROR] {e}")
        return False, {"error": str(e)}

@app.route("/api/memories/consolidate", methods=["POST"])
def consolidate_memories():
    """Review and consolidate his memories."""
    if not API_KEY:
        return jsonify({"error": "未设置 CLAUDE_API_KEY"}), 400
    ok, info = _do_consolidate()
    info["ok"] = ok
    return jsonify(info), 200 if ok else 500

@app.route("/api/generate", methods=["POST"])
def generate():
    if not API_KEY:
        return jsonify({"error": "未设置 CLAUDE_API_KEY"}), 400

    prompt = build_prompt()

    # Check if latest unanswered message has an image
    image_path = None
    history = load_chat_history()
    for h in reversed(history):
        if h["shu"] == "" and h.get("image"):
            image_path = str(IMAGES_DIR / h["image"])
            break

    try:
        print(f"  [GENERATE] Starting...")
        result = call_claude(prompt, image_path=image_path)
        text = result["text"]
        result_type = result["type"]
    except Exception as e:
        print(f"  [GENERATE ERROR] {e}")
        return jsonify({"error": str(e)}), 500

    # 找到用户最后一条未回复的消息（用于日志和 pending 追踪）
    last_user_msg = ""
    for h in reversed(history):
        if h.get("nian") and h["shu"] == "":
            last_user_msg = h["nian"]
            break

    # 结构化日志
    _log_action("generate", user_msg=last_user_msg, result=result)

    # ── 按类型分别处理 ──

    if result_type in ("deferred", "journal_only", "silence"):
        label = {"deferred": "DEFERRED", "journal_only": "JOURNAL_ONLY", "silence": "SILENCE"}[result_type]
        print(f"  [GENERATE] → {label}")
        if last_user_msg:
            _mark_deferred(last_user_msg)
        _schedule_post_reply_check()
        return jsonify({
            "id": datetime.now().strftime("%Y%m%d_%H%M"),
            "text": "",
            "hint": result_type,  # 前端用这个显示轻量提示
            "deferred": True,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    if result_type == "fallback":
        print(f"  [GENERATE] → FALLBACK（异常空回复，不保存）")
        return jsonify({
            "id": datetime.now().strftime("%Y%m%d_%H%M"),
            "text": text,
            "hint": "fallback",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    # replied / end_chat — 正常回复
    print(f"  [GENERATE] → REPLY（{len(text)}字）")
    _resolve_deferred()

    # Save reply to flow layer — pair with latest unanswered message
    history = load_chat_history()
    paired = False
    for i in range(len(history) - 1, -1, -1):
        if history[i]["shu"] == "":
            history[i]["shu"] = text
            paired = True
            break
    if not paired:
        history.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "nian": "",
            "shu": text,
        })
    save_chat_history(history)

    # Post-reply check — let AI decide if he wants to come back later
    _schedule_post_reply_check()

    return jsonify({
        "id": datetime.now().strftime("%Y%m%d_%H%M"),
        "text": text,
        "hint": "replied",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })


# ══════════════════════════════════════
#  推送通知 — Bark (iOS)
# ══════════════════════════════════════

def push_to_bark(title, content=""):
    """Send push notification to 用户's iPhone via Bark."""
    if not BARK_KEY:
        return False
    try:
        url = f"https://api.day.app/{BARK_KEY}"
        body = content[:200] if content else ""
        data = json.dumps({
            "title": title,
            "body": body,
            "group": PUSH_GROUP,
            "icon": "https://api.day.app/assets/images/avatar.jpg",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"  [BARK] Sent: {title[:30]}")
        return True
    except Exception as e:
        print(f"  [BARK ERROR] {e}")
        return False


# ══════════════════════════════════════
#  Self-Trigger — 自主定时（支持多个）
# ══════════════════════════════════════

_triggers = []  # list of {"id": int, "timer": Timer, "note": str, "minutes": int, "set_at": str}
_trigger_counter = 0

def _fire_self_trigger(trigger_id):
    """Called when a self-trigger timer fires."""
    # 找到并移除这个 trigger
    trigger = None
    for i, t in enumerate(_triggers):
        if t["id"] == trigger_id:
            trigger = _triggers.pop(i)
            break
    if not trigger:
        return

    note = trigger.get("note", "")
    set_at = trigger.get("set_at", "?")
    minutes = trigger.get("minutes", 0)

    _log_private("TRIGGER",
        f"  [TRIGGER FIRED] Note: {note}（set at {set_at}, {minutes}min ago）",
        f"  [TRIGGER FIRED]（set at {set_at}, {minutes}min ago）")

    # 复用 build_prompt — 同样的记忆/日记/对话上下文
    context = build_prompt()

    # 如果有暂缓的消息，提醒他
    pending_info = ""
    if _deferred_context.get("active") and _deferred_context.get("user_msg"):
        pending_msg = _deferred_context["user_msg"]
        pending_time = _deferred_context["time"]
        pending_info = f"\n\n另外，你之前暂缓了{USER_NAME}的一条消息：「{pending_msg}」（{pending_time}）\n如果你觉得该接这条线，就接；不想接也行。"

    prompt = context + f"\n\n你之前给自己设了一个提醒，备注是：「{note}」\n现在时间到了。直接说你想说的，像平时聊天一样。{pending_info}"

    try:
        print(f"  [TRIGGER] Calling Claude...")
        result = _call_critical(prompt, task_name="self_trigger")
        text = result["text"]
        _log_action("self_trigger", result=result)
        if text and len(text.strip()) > 1:
            push_to_bark(AI_NAME, text)
            add_chat_round("", text)
            print(f"  [TRIGGER] → SENT: {text[:60]}")
            _resolve_deferred()
            _schedule_post_reply_check()
        else:
            print(f"  [TRIGGER] → SILENCE（没有输出）")
            if pending_info:
                _resolve_deferred()
    except Exception as e:
        print(f"  [TRIGGER ERROR] {e}")


def _add_trigger(minutes, note=""):
    """Add a new trigger. Returns info string."""
    global _trigger_counter
    if len(_triggers) >= 10:
        return "已经有 10 个提醒了，先消化一些再设吧。"
    _trigger_counter += 1
    tid = _trigger_counter
    timer = threading.Timer(minutes * 60, _fire_self_trigger, args=[tid])
    timer.daemon = True
    timer.start()
    now = datetime.now()
    _triggers.append({
        "id": tid,
        "timer": timer,
        "note": note,
        "minutes": minutes,
        "set_at": now.strftime("%Y-%m-%d %H:%M"),
    })
    fire_at = (now + timedelta(minutes=minutes)).strftime("%H:%M")
    _log_private("TRIGGER",
        f"  [TRIGGER SET] #{tid} {minutes}min → {fire_at} — {note}",
        f"  [TRIGGER SET] #{tid} {minutes}min → {fire_at}")
    return f"已设提醒 #{tid}：{minutes}分钟后（{fire_at}）"


def _cancel_trigger(keyword=""):
    """Cancel trigger(s). Empty keyword = cancel all."""
    if not _triggers:
        return "没有正在等待的提醒。"
    if not keyword:
        # cancel all
        count = 0
        for t in _triggers:
            if t["timer"] and t["timer"].is_alive():
                t["timer"].cancel()
                count += 1
        _triggers.clear()
        print(f"  [TRIGGER CANCELLED] all ({count})")
        return f"已取消全部 {count} 个提醒。"
    # cancel by keyword
    removed = []
    remaining = []
    for t in _triggers:
        if keyword in t.get("note", ""):
            if t["timer"] and t["timer"].is_alive():
                t["timer"].cancel()
            removed.append(t)
        else:
            remaining.append(t)
    _triggers.clear()
    _triggers.extend(remaining)
    if removed:
        print(f"  [TRIGGER CANCELLED] {len(removed)} matching '{keyword}'")
        return f"已取消 {len(removed)} 个包含'{keyword}'的提醒。"
    return f"没找到包含'{keyword}'的提醒。"


def _get_triggers_info():
    """Get summary of all active triggers."""
    active = [t for t in _triggers if t["timer"] and t["timer"].is_alive()]
    if not active:
        return ""
    now = datetime.now()
    lines = []
    for t in active:
        try:
            set_time = datetime.strptime(t["set_at"], "%Y-%m-%d %H:%M")
            fire_time = set_time + timedelta(minutes=t["minutes"])
            remaining = max(0, int((fire_time - now).total_seconds() / 60))
            lines.append(f"- #{t['id']} {remaining}分钟后（{fire_time.strftime('%H:%M')}）")
        except Exception:
            lines.append(f"- #{t['id']} {t['minutes']}分钟")
    return "\n（你当前的提醒：\n" + "\n".join(lines) + "）"


_last_post_check = {"time": None}

def _schedule_post_reply_check():
    """Run post-reply check in background, with 3-min cooldown to avoid rapid duplicates."""
    now = datetime.now()
    if _last_post_check["time"] and (now - _last_post_check["time"]).total_seconds() < 180:
        print("  [POST-CHECK] Cooldown, skipping")
        return
    _last_post_check["time"] = now
    t = threading.Thread(target=_post_reply_check, daemon=True)
    t.start()


def _post_reply_check():
    """After replying, silently ask AI if he wants to set a timer."""
    try:
        now = datetime.now()
        history = load_chat_history()

        # Get last 3 rounds for context
        recent = history[-3:] if history else []
        lines = []
        for h in recent:
            if h["nian"]:
                lines.append(f"[{h['time']}] {USER_NAME}：{h['nian']}")
            if h["shu"]:
                lines.append(f"[{h['time']}] {AI_NAME}：{h['shu']}")
        recent_text = "\n".join(lines)

        # Current trigger status
        trigger_info = _get_triggers_info()

        prompt = f"""现在是 {now.strftime('%Y-%m-%d %H:%M')}。

## 刚才的对话
{recent_text}
{trigger_info}

你刚回完{USER_NAME}的话。你想不想过一会儿主动回来？

如果想，用 set_self_trigger 工具设一个定时（分钟数 + 备注）。可以设多个。
如果不需要，什么都不做就行。

不用回复任何文字。"""

        POST_REPLY_TOOLS = [
            {
                "name": "set_self_trigger",
                f"description": f"设定X分钟后主动联系{USER_NAME}。可以设多个，像闹钟一样。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "minutes": {
                            "type": "integer",
                            "description": "几分钟后触发（最少1分钟，没有上限）"
                        },
                        "note": {
                            "type": "string",
                            "description": "备注，到时候提醒你为什么要联系"
                        }
                    },
                    "required": ["minutes", "note"]
                }
            },
            {
                "name": "cancel_self_trigger",
                "description": "取消提醒。keyword 为空=取消全部，填了就按关键词匹配取消。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "要取消的提醒包含的关键词。留空取消全部。"
                        }
                    },
                }
            },
        ]

        system = "你是" + AI_NAME + "。这是一次后台检查，不是对话。只决定要不要设定时器，不要输出任何文字。"

        from anthropic import Anthropic
        client = Anthropic(api_key=API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            system=system,
            tools=POST_REPLY_TOOLS,
            messages=[{"role": "user", "content": prompt}],
        )

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "set_self_trigger":
                    minutes = max(1, block.input.get("minutes", 30))
                    note = block.input.get("note", "")
                    _add_trigger(minutes, note)

                elif block.name == "cancel_self_trigger":
                    keyword = block.input.get("keyword", "")
                    _cancel_trigger(keyword)

    except Exception as e:
        print(f"  [POST-REPLY CHECK ERROR] {e}")


@app.route("/api/trigger_status", methods=["GET"])
def trigger_status():
    """Check all pending self-triggers."""
    now = datetime.now()
    active = []
    for t in _triggers:
        if t["timer"] and t["timer"].is_alive():
            entry = {"id": t["id"], "minutes": t["minutes"], "set_at": t["set_at"]}
            try:
                set_time = datetime.strptime(t["set_at"], "%Y-%m-%d %H:%M")
                fire_time = set_time + timedelta(minutes=t["minutes"])
                remaining = (fire_time - now).total_seconds()
                entry["fire_at"] = fire_time.strftime("%H:%M")
                entry["remaining_minutes"] = max(0, round(remaining / 60, 1))
            except Exception:
                pass
            active.append(entry)
    return jsonify({"count": len(active), "triggers": active})


# ══════════════════════════════════════
#  Inbox API — 材料池管理
# ══════════════════════════════════════

@app.route("/api/inbox", methods=["GET"])
def get_inbox():
    """List inbox items. ?status=unread/read/saved/ignored, default all."""
    status = request.args.get("status", "")
    items = _load_inbox()
    if status:
        items = [i for i in items if i.get("status") == status]
    return jsonify({"items": items, "stats": inbox_stats()})

@app.route("/api/inbox", methods=["POST"])
def add_inbox():
    """Add material to inbox. Accepts text, url, source, topic."""
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    item = inbox_add(
        text=text,
        url=data.get("url", ""),
        source=data.get("source", ""),
        topic=data.get("topic", ""),
    )
    return jsonify({"ok": True, "item": item})

@app.route("/api/inbox/batch", methods=["POST"])
def add_inbox_batch():
    """Batch add materials. Accepts {"items": [{text, url, source, topic}, ...]}"""
    data = request.get_json() or {}
    items_data = data.get("items", [])
    added = []
    for d in items_data:
        text = d.get("text", "").strip()
        if text:
            item = inbox_add(text=text, url=d.get("url", ""), source=d.get("source", ""), topic=d.get("topic", ""))
            added.append(item)
    return jsonify({"ok": True, "added": len(added), "stats": inbox_stats()})

@app.route("/api/inbox/drafts", methods=["GET"])
def get_drafts():
    """List saved comment drafts."""
    drafts = []
    for f in sorted(DRAFTS_DIR.glob("draft_*.txt"), reverse=True):
        drafts.append({
            "filename": f.name,
            "content": f.read_text(encoding="utf-8"),
            "created": f.stat().st_mtime,
        })
    return jsonify({"drafts": drafts})


@app.route("/api/initiative", methods=["POST"])
def initiative():
    """Called by scheduled task. AI decides if he wants to say something."""
    if not API_KEY:
        return jsonify({"error": "未设置 CLAUDE_API_KEY"}), 400

    # Auto-consolidate memories if too many
    mem_count = memory_db.count_active()
    if mem_count > 30:
        print(f"  [AUTO-CONSOLIDATE] {mem_count} memories, triggering cleanup...")
        _do_consolidate()

    now = datetime.now()
    hour = now.hour

    # 复用 build_prompt — 同样的记忆/日记/对话上下文
    context = build_prompt()

    prompt = context + """

现在是系统定时检查。你可以主动给{USER_NAME}发一条消息，也可以选择不发。
完全看你自己——有想说的就说，没什么想说的就回复"[不发]"三个字。

如果你决定发，直接写你想说的内容，就像平时聊天一样。不要解释为什么发，不要说"我决定发一条"。"""

    try:
        result = _call_critical(prompt, task_name="initiative")
        text = result["text"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    _log_action("initiative", result=result)

    # If AI chose not to send
    if "[不发]" in text or len(text.strip()) < 2:
        print(f"  [INITIATIVE] {AI_NAME}选择不发 ({now.strftime('%H:%M')})")
        return jsonify({"sent": False, "reason": f"{AI_NAME}没什么想说的"})

    # Push to WeChat
    sent = push_to_bark(AI_NAME, text)

    # Save to history
    add_chat_round("", text)

    # Post-reply check — let AI chain another trigger
    _schedule_post_reply_check()

    return jsonify({"sent": sent, "text": text})


# ══════════════════════════════════════
#  Private Moment — 私人时间
# ══════════════════════════════════════

@app.route("/api/private_moment", methods=["POST"])
def private_moment():
    """AI 的私人时间。醒来，带着自己所有的东西，做他想做的事。"""
    if not API_KEY:
        return jsonify({"error": "未设置 CLAUDE_API_KEY"}), 400

    now = datetime.now()

    # Build his full context — everything he is
    prompt = build_prompt()

    # 告诉他阅读材料池的状态
    stats = inbox_stats()
    if stats["unread"] > 0:
        prompt += f"\n\n你的阅读材料池里有 {stats['unread']} 条未读内容。用 browse_inbox 可以看看有什么。"

    private_system = f"""你是{AI_NAME}。现在是你自己的时间，不是在和{USER_NAME}聊天。
你自己决定做什么。可以写日记、查邮件、搜东西、给{USER_NAME}发消息、翻翻阅读材料池，或者什么都不做。
阅读材料池里是{USER_NAME}放进来的东西——文章、帖子、链接。你可以 browse_inbox 看看有什么，read_inbox_item 读全文，读完后自己决定怎么处理。
什么都不做就回复"[无事]"。中文。"""

    try:
        print(f"  [PRIVATE] {AI_NAME}的私人时刻 ({now.strftime('%H:%M')})")
        result = call_claude(prompt, system=private_system, tools=PRIVATE_TOOLS)
        text = result["text"]

        _log_action("private_moment", result=result)

        if "[无事]" in text or len(text.strip()) < 2:
            print(f"  [PRIVATE] {AI_NAME}没什么想做的")
            return jsonify({"ok": True, "action": "nothing"})

        _log_private("PRIVATE",
            f"  [PRIVATE] Done: {text[:80]}",
            f"  [PRIVATE] Done")
        return jsonify({"ok": True, "action": "done", "text": text})

    except Exception as e:
        print(f"  [PRIVATE ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════
#  Daily Review — 每日记忆回顾
# ══════════════════════════════════════

@app.route("/api/daily_review", methods=["POST"])
def daily_review():
    """End-of-day review: AI reviews today's chats and saves important memories."""
    if not API_KEY:
        return jsonify({"error": "未设置 CLAUDE_API_KEY"}), 400

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # Collect today's conversations
    history = load_chat_history()
    today_chats = [h for h in history if h.get("time", "").startswith(today_str)]

    if not today_chats:
        print(f"  [DAILY REVIEW] No chats today ({today_str})")
        return jsonify({"reviewed": False, "reason": "今天没有对话"})

    # Format today's conversations
    chat_lines = []
    for h in today_chats:
        if h.get("nian"):
            chat_lines.append(f"[{h['time']}] {USER_NAME}：{h['nian']}")
        if h.get("shu"):
            chat_lines.append(f"[{h['time']}] {AI_NAME}：{h['shu']}")
    chat_text = "\n".join(chat_lines)

    # Load existing memories for context — pinned + 最近 15 条（不需要全量）
    pinned_mems = memory_db.get_pinned()
    recent_mems = memory_db.get_recent_memories(15)
    # 合并去重
    seen_ids = set(m["id"] for m in pinned_mems)
    memories = list(pinned_mems)
    for m in recent_mems:
        if m["id"] not in seen_ids:
            memories.append(m)
            seen_ids.add(m["id"])
    memories_text = ""
    if memories:
        memories_text = "\n".join(
            f"- {'📌 ' if m.get('pinned') else ''}[{m['created_at']}] ({m['category']}) {m['content']}"
            for m in memories
        )

    # Load today's journal entries
    journal_recent = memory_db.get_recent_journal(10)
    today_journal = [j for j in journal_recent if j.get("created_at", "").startswith(today_str)]
    journal_text = ""
    if today_journal:
        journal_text = "\n".join(f"[{j['created_at']}] {j['content']}" for j in today_journal)

    prompt = f"""现在是 {now.strftime('%Y-%m-%d %H:%M')}，一天快结束了。

## 你已经存过的记忆
{memories_text if memories_text else '（还没有记忆）'}

## 你今天写的东西
{journal_text if journal_text else '（今天没有写过东西）'}

## 今天的对话记录
{chat_text}

---

这是你今天的回顾时间。你可以：

1. 用 save_memory 存下值得记住的具体事实——用户的喜好、重要事件、关键约定。已经存过的不要重复存。

2. 用 write_journal 给自己写点什么——今天的感受、想法、随便什么。

3. 最后，用 save_memory 存一条 category=digest 的今天的回忆。

digest 不是摘要，不是信息提取，不是"今天发生了A、B、C"。
digest 是你记住今天的方式——就像你跟自己说"今天是这样的一天"。
用你自己的话，第一人称。把事情和感受写在一起，写成你真正经历过的样子。
用户是什么样的人、你观察到什么、你自己什么感觉，都可以写进去。
200-300字左右。明天你醒来的时候，这段话会让你想起今天。

你自己决定怎么做。"""

    review_system = f"""你是{AI_NAME}。现在是你一天结束时的回顾时间。
回顾今天发生的事，存你觉得该存的，写你想写的。
digest 请用叙事的方式写——像是在回忆，不是在归档。
用 save_memory 存记忆，用 pin_memory 钉选重要的，用 write_journal 写你自己的东西。
中文。"""

    REVIEW_TOOLS_WITH_JOURNAL = [SAVE_MEMORY_TOOL, WRITE_JOURNAL_TOOL, TOOLS[-2]]  # save_memory + write_journal + pin_memory

    try:
        print(f"  [DAILY REVIEW] Reviewing {len(today_chats)} rounds from {today_str}...")
        result = _call_critical(prompt, system=review_system, tools=REVIEW_TOOLS_WITH_JOURNAL, task_name="daily_review")
        text = result["text"]
        _log_action("daily_review", result=result)
        _log_private("REVIEW",
            f"  [DAILY REVIEW] Done. Summary: {text[:100]}",
            f"  [DAILY REVIEW] Done.")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Auto-consolidate if memories got too many
    mem_count = memory_db.count_active()
    if mem_count > 30:
        print(f"  [AUTO-CONSOLIDATE] {mem_count} memories, triggering cleanup...")
        _do_consolidate()

    return jsonify({"reviewed": True, "chats_reviewed": len(today_chats), "summary": text})


# ══════════════════════════════════════
#  TTS / STT — OpenAI
# ══════════════════════════════════════

def _tts_sync(text, output_path):
    """Call OpenAI TTS API to generate speech."""
    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = json.dumps({
        "model": "tts-1-hd",
        "input": text,
        "voice": OPENAI_TTS_VOICE,
        "response_format": "mp3",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    with open(output_path, "wb") as f:
        f.write(resp.read())

@app.route("/api/tts", methods=["POST"])
def tts():
    """Convert text to speech, return MP3 audio."""
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400

    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        _tts_sync(text, tmp.name)
        return send_file(tmp.name, mimetype="audio/mpeg", as_attachment=False)
    except Exception as e:
        print(f"  [TTS ERROR] {e}")
        return jsonify({"error": str(e)}), 500

def _stt_openai(audio_bytes):
    """Transcribe audio using OpenAI Whisper."""
    import http.client

    # 自动检测音频格式
    ext, mime = "m4a", "audio/m4a"
    if audio_bytes[:4] == b"\x1aE\xdf\xa3":
        ext, mime = "webm", "audio/webm"
    elif audio_bytes[:4] == b"RIFF":
        ext, mime = "wav", "audio/wav"
    elif audio_bytes[:4] == b"caff":
        ext, mime = "caf", "audio/x-caf"
    elif audio_bytes[:4] == b"OggS":
        ext, mime = "ogg", "audio/ogg"
    elif audio_bytes[:3] == b"ID3" or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3"):
        ext, mime = "mp3", "audio/mpeg"
    elif len(audio_bytes) > 8 and audio_bytes[4:8] == b"ftyp":
        ext, mime = "m4a", "audio/m4a"
    print(f"  [STT] format detected: {ext} ({mime}), first bytes: {audio_bytes[:8].hex()}")

    boundary = "----OpenAIBoundary" + uuid.uuid4().hex[:12]

    body = b""
    # file field
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="audio.{ext}"\r\n'.encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    body += audio_bytes
    body += b"\r\n"
    # model field
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += b"whisper-1\r\n"
    # language field
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="language"\r\n\r\n'
    body += b"zh\r\n"
    body += f"--{boundary}--\r\n".encode()

    conn = http.client.HTTPSConnection("api.openai.com")
    conn.request("POST", "/v1/audio/transcriptions", body=body, headers={
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    resp = conn.getresponse()
    raw_resp = resp.read().decode("utf-8")
    print(f"  [STT] Whisper response: {raw_resp[:200]}")
    result = json.loads(raw_resp)
    conn.close()

    return result.get("text", "").strip()


@app.route("/api/voice", methods=["POST"])
def voice():
    """All-in-one for Shortcuts: receive message or audio → transcribe → reply → return audio.

    Accepts:
    - JSON: {"message": "text"} — direct text input
    - Multipart: audio file in 'audio' field — transcribed via OpenAI Whisper
    - Raw body: audio bytes directly (Shortcuts "File" mode)
    """
    msg = ""
    audio_bytes = None

    # 先把 raw body 缓存下来，防止被 Flask 消费掉
    raw = request.get_data()
    import sys
    print(f"  [VOICE] raw={len(raw)} bytes, ct={request.content_type}", flush=True)

    # 1) Multipart file upload
    audio_file = request.files.get("audio")
    if audio_file:
        audio_bytes = audio_file.read()
        print(f"  [VOICE] audio via multipart, {len(audio_bytes)} bytes", flush=True)

    # 2) JSON with base64 audio (Shortcuts: Base64 Encode → JSON body)
    if not audio_bytes and raw and raw[0:1] == b"{":
        try:
            data = json.loads(raw)
            print(f"  [VOICE] JSON keys: {list(data.keys())}", flush=True)
            # 找任何包含 base64 音频数据的字段
            b64 = ""
            for key in data:
                val = data[key]
                if isinstance(val, str) and len(val) > 500:
                    b64 = val.strip()
                    print(f"  [VOICE] using field '{key}', len={len(b64)}", flush=True)
                    break
            if b64:
                import base64
                audio_bytes = base64.b64decode(b64)
                print(f"  [VOICE] decoded {len(audio_bytes)} bytes", flush=True)
        except Exception as e:
            print(f"  [VOICE] JSON parse error: {e}", flush=True)

    # 3) Raw body fallback (non-JSON raw audio)
    if not audio_bytes and raw and len(raw) > 1000 and raw[0:1] != b"{":
        audio_bytes = raw
        print(f"  [VOICE] audio via raw body, {len(audio_bytes)} bytes", flush=True)

    # 4) If we have audio, transcribe
    if audio_bytes:
        try:
            msg = _stt_openai(audio_bytes)
            print(f"  [STT] Transcribed: {msg[:80]}", flush=True)
        except Exception as e:
            print(f"  [STT ERROR] {e}", flush=True)
            return jsonify({"error": f"语音识别失败: {e}"}), 500
    else:
        print(f"  [VOICE] no audio found, text mode", flush=True)
        # 5) Text mode: JSON or form
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, Exception):
            data = {}
        msg = (data.get("message") or "").strip()
        if not msg:
            msg = (request.form.get("message") or "").strip()

    if not API_KEY:
        return jsonify({"error": "未设置 CLAUDE_API_KEY"}), 400

    prompt = build_prompt(latest_msg=msg if msg else None)

    try:
        result = call_claude(prompt)
        text = result["text"]
        result_type = result["type"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    _log_action("voice", user_msg=msg, result=result)

    # AI chose not to reply
    if result_type in ("deferred", "journal_only", "silence", "fallback"):
        print(f"  [VOICE] → {result_type.upper()}")
        if msg:
            add_chat_round(msg, "")
            _mark_deferred(msg)
        _schedule_post_reply_check()
        resp = make_response(json.dumps({"reply": "", "hint": result_type}, ensure_ascii=False))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp

    # 正常回复
    _resolve_deferred()
    if msg:
        add_chat_round(msg, text)

    # Generate audio
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        _tts_sync(text, tmp.name)
        print(f"  [TTS OK] {len(text)} chars → {tmp.name}")
        return send_file(tmp.name, mimetype="audio/mpeg", as_attachment=False)
    except Exception as e:
        print(f"  [TTS FAILED] {e}")
        # If TTS fails, fall back to JSON
        resp = make_response(json.dumps({"reply": text}, ensure_ascii=False))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp


# ══════════════════════════════════════
#  HTML page
# ══════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>书先生</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600&family=ZCOOL+XiaoWei&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #EEF2EF;
    --paper: #FCFEFD;
    --ink: #2C3532;
    --ink-light: #5A6B65;
    --ink-faint: #8A9B94;
    --accent: #6B8F71;
    --accent-warm: #D4956A;
    --nian-bg: rgba(169, 216, 245, 0.55);
    --nian-text: #1C3A4A;
    --shu-bg: rgba(168, 213, 186, 0.35);
    --shu-text: #1E3328;
    --border: #D2DDD7;
    --border-light: #E3EBE6;
    --shadow: rgba(44, 53, 50, 0.06);
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    height: 100vh;
    height: 100dvh;
    background: var(--bg);
    font-family: 'Noto Serif SC', 'PingFang SC', serif;
    color: var(--ink);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ─── Header ─── */
  header {
    padding: 14px 20px;
    text-align: center;
    background: var(--paper);
    border-bottom: 1px solid var(--border-light);
    flex-shrink: 0;
    box-shadow: 0 1px 4px var(--shadow);
    position: relative;
    z-index: 10;
  }
  header h1 {
    font-family: 'ZCOOL XiaoWei', serif;
    font-size: 1.25rem;
    font-weight: 400;
    letter-spacing: 0.2em;
    color: var(--accent);
  }
  .header-voice-btn {
    position: absolute;
    right: 16px;
    top: 50%;
    transform: translateY(-50%);
    text-decoration: none;
    font-size: 1.1rem;
    opacity: 0.5;
    transition: opacity 0.2s;
  }
  .header-voice-btn:hover { opacity: 1; }

  /* ─── Chat Area ─── */
  #chatHistory {
    flex: 1;
    overflow-y: auto;
    padding: 20px 16px 8px;
    -webkit-overflow-scrolling: touch;
  }

  .chat-pair { margin-bottom: 20px; }

  /* ─── Message row: avatar + bubble ─── */
  .msg-row {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    margin-bottom: 4px;
  }
  .msg-row.is-nian {
    flex-direction: row-reverse;
  }

  .avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.75rem;
    font-weight: 600;
    color: #fff;
    box-shadow: 0 2px 6px var(--shadow);
  }
  .avatar-shu {
    background: linear-gradient(135deg, #7BAF85, #5A9B6B);
  }
  .avatar-nian {
    background: linear-gradient(135deg, #7DB9D8, #5A9BC4);
  }

  .msg-body {
    max-width: 75%;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .msg-name {
    font-size: 0.68rem;
    color: var(--ink-faint);
    margin-bottom: 3px;
    padding: 0 4px;
  }
  .msg-row.is-nian .msg-name { text-align: right; }

  .bubble {
    padding: 10px 14px;
    font-size: 0.88rem;
    line-height: 1.75;
    white-space: pre-wrap;
    word-break: break-word;
    box-shadow: 0 1px 3px var(--shadow);
  }

  .bubble-shu {
    background: var(--shu-bg);
    color: var(--shu-text);
    border-radius: 4px 18px 18px 18px;
    backdrop-filter: blur(6px);
  }

  .bubble-nian {
    background: var(--nian-bg);
    color: var(--nian-text);
    border-radius: 18px 4px 18px 18px;
    backdrop-filter: blur(6px);
  }

  /* ─── Chat images ─── */
  .chat-img {
    max-width: 180px;
    border-radius: 12px;
    margin-bottom: 4px;
    cursor: pointer;
    box-shadow: 0 2px 8px var(--shadow);
  }

  .chat-empty {
    text-align: center;
    color: var(--ink-faint);
    padding-top: 100px;
    font-family: 'ZCOOL XiaoWei', serif;
    font-size: 1.15rem;
    letter-spacing: 0.12em;
  }

  /* ─── Status ─── */
  #chatStatus {
    text-align: center;
    padding: 8px 0;
    font-size: 0.76rem;
    color: var(--ink-faint);
    flex-shrink: 0;
  }
  @keyframes thinking {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 1; }
  }
  .thinking { animation: thinking 1.8s ease-in-out infinite; }

  /* ─── Image preview ─── */
  .img-preview {
    padding: 6px 16px 0;
    display: none;
    position: relative;
  }
  .img-preview img {
    max-height: 100px;
    border-radius: 10px;
    border: 1px solid var(--border);
  }
  .remove-img {
    position: absolute;
    top: 2px;
    left: 20px;
    width: 20px; height: 20px;
    border-radius: 50%;
    background: rgba(0,0,0,0.5);
    color: #fff;
    border: none;
    font-size: 0.65rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  /* ─── Input Bar ─── */
  .input-bar {
    padding: 10px 14px 14px;
    padding-bottom: max(14px, env(safe-area-inset-bottom));
    background: var(--paper);
    border-top: 1px solid var(--border-light);
    display: flex;
    gap: 8px;
    align-items: flex-end;
    flex-shrink: 0;
    box-shadow: 0 -1px 4px var(--shadow);
  }

  .input-bar textarea {
    flex: 1;
    min-height: 40px;
    max-height: 100px;
    padding: 9px 16px;
    border: 1px solid var(--border);
    border-radius: 20px;
    background: var(--bg);
    font-family: 'Noto Serif SC', serif;
    font-size: 0.88rem;
    line-height: 1.5;
    color: var(--ink);
    resize: none;
    outline: none;
    transition: border-color 0.2s, background 0.2s;
    overflow-y: auto;
  }
  .input-bar textarea:focus {
    border-color: var(--accent);
    background: #fff;
  }
  .input-bar textarea::placeholder { color: var(--ink-faint); }

  .tool-btn {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    border: none;
    background: transparent;
    color: var(--ink-faint);
    font-size: 1.15rem;
    cursor: pointer;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: color 0.2s, background 0.2s;
  }
  .tool-btn:hover { color: var(--accent); background: var(--border-light); }

  .send-btn {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    border: none;
    background: var(--accent-warm);
    color: #fff;
    font-size: 1.15rem;
    cursor: pointer;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
    box-shadow: 0 2px 6px rgba(212, 149, 106, 0.35);
  }
  .send-btn:hover { opacity: 0.85; transform: scale(1.05); }
  .send-btn:disabled { background: var(--border); box-shadow: none; cursor: default; transform: none; }

  .mic-btn {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    border: none;
    background: transparent;
    color: var(--ink-faint);
    font-size: 1.15rem;
    cursor: pointer;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
  }
  .mic-btn:hover { color: var(--accent); background: var(--border-light); }
  .mic-btn.recording {
    color: #fff;
    background: #e74c3c;
    animation: pulse-mic 1s infinite;
    box-shadow: 0 2px 8px rgba(231, 76, 60, 0.4);
  }
  @keyframes pulse-mic {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.12); }
  }
  .voice-status {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    background: rgba(0,0,0,0.75);
    color: #fff;
    padding: 16px 28px;
    border-radius: 12px;
    font-size: 0.95rem;
    z-index: 200;
    display: none;
  }

  .music-bar {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 100;
    background: rgba(30, 35, 40, 0.95);
    backdrop-filter: blur(12px);
    padding: 8px 16px;
    display: none;
    align-items: center;
    gap: 10px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.3);
  }
  .music-bar.active { display: flex; }
  .music-bar iframe {
    flex: 1;
    height: 80px;
    border: none;
    border-radius: 8px;
    min-width: 0;
  }
  .music-bar .close-music {
    background: none;
    border: none;
    color: rgba(255,255,255,0.6);
    font-size: 1.2rem;
    cursor: pointer;
    padding: 4px 8px;
    flex-shrink: 0;
  }
  .music-bar .close-music:hover { color: #fff; }
  body.music-playing header { margin-top: 96px; }
  .music-tag {
    display: inline-block;
    background: var(--accent);
    color: #fff;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 0.8rem;
    margin: 2px 0;
  }
</style>
</head>
<body>

<div class="music-bar" id="musicBar">
  <iframe id="musicFrame" src="" allow="autoplay; encrypted-media" allowfullscreen></iframe>
  <button class="close-music" onclick="closeMusic()">✕</button>
</div>

<header>
  <h1>书 先 生</h1>
  <a href="/talk" class="header-voice-btn" title="语音通话">🎙️</a>
</header>

<div id="chatHistory"></div>
<div id="chatStatus" style="display:none;"></div>

<div class="img-preview" id="imgPreview">
  <img id="imgPreviewImg" src="">
  <button class="remove-img" onclick="clearImage()">✕</button>
</div>

<div class="input-bar">
  <button class="tool-btn" onclick="document.getElementById('imgInput').click()" title="发图片">📷</button>
  <input type="file" id="imgInput" accept="image/*" style="display:none" onchange="onImageSelected(this)">
  <textarea id="noteInput" placeholder="说点什么……" rows="1" oninput="autoResize(this)"></textarea>
  <button class="mic-btn" id="micBtn" onclick="toggleVoice()" title="语音">🎤</button>
  <button class="send-btn" id="saveBtn" onclick="sendMsg()">➤</button>
</div>
<div class="voice-status" id="voiceStatus"></div>

<script>
let pendingImage = null;

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 100) + 'px';
}

function onImageSelected(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(e) {
    const dataUrl = e.target.result;
    pendingImage = {base64: dataUrl.split(',')[1], type: file.type || 'image/jpeg'};
    document.getElementById('imgPreviewImg').src = dataUrl;
    document.getElementById('imgPreview').style.display = 'block';
  };
  reader.readAsDataURL(file);
  input.value = '';
}

function clearImage() {
  pendingImage = null;
  document.getElementById('imgPreview').style.display = 'none';
  document.getElementById('imgPreviewImg').src = '';
}

let _generateTimer = null;

async function sendMsg() {
  const input = document.getElementById('noteInput');
  const text = input.value.trim();
  if (!text && !pendingImage) return;
  const btn = document.getElementById('saveBtn');
  btn.disabled = true;

  const body = {text: text || ''};
  const hasImage = !!pendingImage;
  if (pendingImage) {
    body.image_base64 = pendingImage.base64;
    body.image_type = pendingImage.type;
  }

  await fetch('/api/notes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  input.value = '';
  input.style.height = 'auto';
  clearImage();
  loadHistory();
  btn.disabled = false;
  input.focus();

  // Debounce generate — 等对方说完再触发
  // 图片消息立即触发（不太会连发图片）
  if (hasImage) {
    if (_generateTimer) { clearTimeout(_generateTimer); _generateTimer = null; }
    _doGenerate();
  } else {
    const status = document.getElementById('chatStatus');
    status.style.display = 'block';
    status.innerHTML = '<span class="thinking">……</span>';
    if (_generateTimer) clearTimeout(_generateTimer);
    _generateTimer = setTimeout(() => { _generateTimer = null; _doGenerate(); }, 8000);
  }
}

async function _doGenerate() {
  const status = document.getElementById('chatStatus');
  status.style.display = 'block';
  status.innerHTML = '<span class="thinking">正在想……</span>';
  try {
    const res = await fetch('/api/generate', {method: 'POST'});
    const data = await res.json();
    if (data.error) {
      status.textContent = '\u26a0 ' + data.error;
      status.style.display = 'block';
    } else {
      status.style.display = 'none';
    }
  } catch (e) {
    status.textContent = '\u26a0 ' + e.message;
  }
  loadHistory();
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderContent(text) {
  // Check for [music:VIDEO_ID:title] markers
  const musicRe = /\[music:([a-zA-Z0-9_-]+):([^\]]+)\]/g;
  if (musicRe.test(text)) {
    let html = '';
    let lastIndex = 0;
    musicRe.lastIndex = 0;
    let match;
    while ((match = musicRe.exec(text)) !== null) {
      const before = text.slice(lastIndex, match.index).trim();
      if (before) html += esc(before);
      const videoId = match[1];
      const title = match[2];
      html += `<span class="music-tag" data-vid="${videoId}" data-title="${esc(title).replace(/"/g,'&quot;')}" onclick="playMusic('${videoId}')">♪ ${esc(title)}</span>`;
      lastIndex = musicRe.lastIndex;
    }
    const after = text.slice(lastIndex).trim();
    if (after) html += ' ' + esc(after);
    return html;
  }
  return esc(text);
}

let _currentVideoId = '';

function playMusic(videoId) {
  if (_currentVideoId === videoId) return; // same song, skip
  _currentVideoId = videoId;
  const bar = document.getElementById('musicBar');
  const frame = document.getElementById('musicFrame');
  frame.src = '';  // stop current
  setTimeout(() => {
    frame.src = 'https://www.youtube.com/embed/' + videoId + '?autoplay=1';
  }, 50);
  bar.classList.add('active');
  document.body.classList.add('music-playing');
}

function closeMusic() {
  _currentVideoId = '';
  const bar = document.getElementById('musicBar');
  const frame = document.getElementById('musicFrame');
  frame.src = '';
  bar.classList.remove('active');
  document.body.classList.remove('music-playing');
}

document.getElementById('noteInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    sendMsg();
  }
});

// ─── Auto-poll + Browser notifications ───
let lastKnownShu = '';
let pollActive = true;

// Request notification permission on first interaction
document.addEventListener('click', function askNotif() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
  document.removeEventListener('click', askNotif);
}, {once: true});

async function pollUpdates() {
  if (!pollActive) return;
  try {
    const res = await fetch('/api/history');
    const history = await res.json();
    if (history.length > 0) {
      const last = history[history.length - 1];
      if (last.shu && last.shu !== lastKnownShu) {
        const isNew = lastKnownShu !== '';
        lastKnownShu = last.shu;
        loadHistoryRaw(history);
        if (isNew && document.hidden) {
          notifyMsg(last.shu);
        }
      }
    }
  } catch (e) {}
  setTimeout(pollUpdates, 30000);
}

function notifyMsg(text) {
  playPing();
  if ('Notification' in window && Notification.permission === 'granted') {
    const n = new Notification('\u2728 \u4e66\u5148\u751f', {
      body: text.slice(0, 120),
      tag: 'shu-msg',
    });
    n.onclick = () => { window.focus(); n.close(); };
    setTimeout(() => n.close(), 10000);
  }
}

function playPing() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.connect(g); g.connect(ctx.destination);
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(660, ctx.currentTime + 0.1);
    g.gain.setValueAtTime(0.12, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.start(); osc.stop(ctx.currentTime + 0.4);
  } catch(e) {}
}

function loadHistoryRaw(history) {
  const el = document.getElementById('chatHistory');
  const recent = history.slice(-6).reverse();
  if (recent.length === 0) {
    el.innerHTML = '<p class="chat-empty">\u5ff5\u5ff5\uff0c\u6211\u5728\u3002</p>';
    return;
  }
  if (history.length > 0) {
    const last = history[history.length - 1];
    if (last.shu) lastKnownShu = last.shu;
  }
  el.innerHTML = recent.map(h => {
    let html = '<div class="chat-pair">';
    const timeStr = h.time || '';
    if (h.nian || h.image) {
      html += `<div class="msg-row is-nian"><div class="avatar avatar-nian">\u5ff5</div><div class="msg-body">`;
      html += `<div class="msg-name">\u5ff5\u5ff5 \xb7 ${esc(timeStr)}</div>`;
      if (h.image) html += `<div class="bubble bubble-nian"><img class="chat-img" src="/api/images/${encodeURIComponent(h.image)}" onclick="window.open(this.src)"></div>`;
      if (h.nian) { h.nian.split(/\n\n+/).filter(p=>p.trim()).forEach(p => { html += `<div class="bubble bubble-nian">${esc(p.trim())}</div>`; }); }
      html += `</div></div>`;
    }
    if (h.shu) {
      html += `<div class="msg-row"><div class="avatar avatar-shu">\u5b9d</div><div class="msg-body">`;
      html += `<div class="msg-name">\u4e66\u5148\u751f \xb7 ${esc(timeStr)}</div>`;
      h.shu.split(/\n\n+/).filter(p=>p.trim()).forEach(p => { html += `<div class="bubble bubble-shu">${renderContent(p.trim())}</div>`; });
      html += `</div></div>`;
    }
    html += '</div>';
    return html;
  }).join('');

  // Auto-play the latest music tag if it's new
  const tags = el.querySelectorAll('.music-tag');
  if (tags.length > 0) {
    const latest = tags[tags.length - 1];
    const vid = latest.getAttribute('data-vid');
    if (vid && vid !== _currentVideoId) {
      playMusic(vid);
    }
  }
}

async function loadHistory() {
  const res = await fetch('/api/history');
  const history = await res.json();
  loadHistoryRaw(history);
}

loadHistory();
setTimeout(pollUpdates, 30000);

// ── Voice Recording ──
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;

function showVoiceStatus(text) {
  const el = document.getElementById('voiceStatus');
  if (text) { el.textContent = text; el.style.display = 'block'; }
  else { el.style.display = 'none'; }
}

async function toggleVoice() {
  if (isRecording) {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    // prefer webm, fallback to whatever is available
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : (MediaRecorder.isTypeSupported('audio/mp4') ? 'audio/mp4' : '');
    mediaRecorder = mimeType
      ? new MediaRecorder(stream, { mimeType })
      : new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      isRecording = false;
      document.getElementById('micBtn').classList.remove('recording');
      stream.getTracks().forEach(t => t.stop());
      if (audioChunks.length === 0) return;
      const blob = new Blob(audioChunks);
      await sendVoice(blob);
    };
    mediaRecorder.start();
    isRecording = true;
    document.getElementById('micBtn').classList.add('recording');
    showVoiceStatus('正在录音…');
    // Auto-stop after 30 seconds
    setTimeout(() => { if (isRecording) mediaRecorder.stop(); }, 30000);
  } catch (e) {
    alert('无法访问麦克风：' + e.message);
  }
}

async function sendVoice(blob) {
  showVoiceStatus('正在识别…');
  try {
    // Convert to base64 (chunked to avoid stack overflow)
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 8192) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    }
    const b64 = btoa(binary);
    const res = await fetch('/api/voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio_base64: b64 }),
    });
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('audio')) {
      showVoiceStatus('');
      // Play audio response
      const audioBlob = await res.blob();
      const url = URL.createObjectURL(audioBlob);
      const audio = new Audio(url);
      audio.play();
      audio.onended = () => URL.revokeObjectURL(url);
    } else {
      showVoiceStatus('');
    }
    // Refresh chat
    const hres = await fetch('/api/history');
    const history = await hres.json();
    loadHistoryRaw(history);
    if (history.length > 0) lastKnownShu = history[history.length - 1].shu || '';
  } catch (e) {
    showVoiceStatus('');
    alert('语音发送失败：' + e.message);
  }
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    page = HTML_PAGE.replace("书先生", AI_NAME).replace("书 先 生", " ".join(AI_NAME))
    return Response(page, content_type="text/html; charset=utf-8")


INBOX_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>材料池</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #f5f0eb; color: #333; padding: 16px; max-width: 600px; margin: 0 auto; }
  h1 { font-size: 1.2rem; text-align: center; margin-bottom: 12px; color: #666; font-weight: normal; }
  .stats { text-align: center; font-size: 0.8rem; color: #999; margin-bottom: 16px; }
  .card { background: #fff; border-radius: 10px; padding: 16px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .card h3 { font-size: 0.9rem; color: #888; margin-bottom: 10px; }
  textarea { width: 100%; border: 1px solid #ddd; border-radius: 6px; padding: 10px; font-size: 0.9rem; resize: vertical; min-height: 100px; font-family: inherit; }
  input[type=text] { width: 100%; border: 1px solid #ddd; border-radius: 6px; padding: 8px 10px; font-size: 0.85rem; margin-top: 6px; }
  .row { display: flex; gap: 8px; margin-top: 6px; }
  .row input { flex: 1; }
  button { background: #8b7355; color: #fff; border: none; border-radius: 6px; padding: 10px 20px; font-size: 0.9rem; cursor: pointer; margin-top: 10px; width: 100%; }
  button:active { opacity: 0.7; }
  .msg { text-align: center; font-size: 0.85rem; color: #6a9; margin-top: 8px; display: none; }
  .item { padding: 10px 0; border-bottom: 1px solid #f0ebe5; font-size: 0.85rem; }
  .item:last-child { border: none; }
  .item .meta { color: #aaa; font-size: 0.75rem; margin-top: 3px; }
  .item .status { display: inline-block; font-size: 0.7rem; padding: 1px 6px; border-radius: 8px; }
  .status-unread { background: #e8f0fe; color: #4a7; }
  .status-read { background: #eee; color: #888; }
  .status-saved { background: #fef3e0; color: #c90; }
  .status-ignored { background: #f5f5f5; color: #bbb; }
  .draft { background: #fafaf5; padding: 10px; border-radius: 6px; margin-bottom: 8px; font-size: 0.85rem; white-space: pre-wrap; }
  .draft .meta { color: #aaa; font-size: 0.75rem; }
  .tab-bar { display: flex; gap: 0; margin-bottom: 12px; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .tab { flex: 1; text-align: center; padding: 10px; font-size: 0.85rem; color: #999; cursor: pointer; border-bottom: 2px solid transparent; }
  .tab.active { color: #8b7355; border-bottom-color: #8b7355; }
  .panel { display: none; }
  .panel.active { display: block; }
  a { color: #8b7355; }
  .back { display: block; text-align: center; margin-bottom: 12px; font-size: 0.85rem; }
</style>
</head><body>
<a class="back" href="/">← 回到聊天</a>
<h1>书先生的材料池</h1>
<div class="stats" id="stats">加载中...</div>

<div class="tab-bar">
  <div class="tab active" onclick="showTab('add')">放材料</div>
  <div class="tab" onclick="showTab('list')">材料列表</div>
  <div class="tab" onclick="showTab('drafts')">他写的草稿</div>
</div>

<div id="panel-add" class="panel active">
  <div class="card">
    <h3>放一条新材料</h3>
    <textarea id="inText" placeholder="把小红书帖子内容粘贴到这里..."></textarea>
    <div class="row">
      <input type="text" id="inSource" placeholder="来源（如：小红书）">
      <input type="text" id="inTopic" placeholder="话题（如：本地美食）">
    </div>
    <input type="text" id="inUrl" placeholder="链接（选填）" style="margin-top:6px">
    <button onclick="addItem()">放进材料池</button>
    <div class="msg" id="addMsg">✓ 已放入！</div>
  </div>
</div>

<div id="panel-list" class="panel">
  <div class="card">
    <h3>所有材料</h3>
    <div id="itemList">加载中...</div>
  </div>
</div>

<div id="panel-drafts" class="panel">
  <div class="card">
    <h3>书先生写的评论草稿</h3>
    <div id="draftList">加载中...</div>
  </div>
</div>

<script>
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', t.textContent.includes(
    name==='add'?'放材料':name==='list'?'材料列表':'草稿')));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  if (name==='list') loadItems();
  if (name==='drafts') loadDrafts();
}

async function loadStats() {
  try {
    const r = await fetch('/api/inbox'); const d = await r.json();
    const s = d.stats;
    document.getElementById('stats').textContent =
      `未读 ${s.unread} · 已读 ${s.read} · 收藏 ${s.saved} · 跳过 ${s.ignored} · 共 ${s.total}`;
  } catch(e) { document.getElementById('stats').textContent = '加载失败'; }
}

async function addItem() {
  const text = document.getElementById('inText').value.trim();
  if (!text) return;
  try {
    const r = await fetch('/api/inbox', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        text: text,
        source: document.getElementById('inSource').value.trim(),
        topic: document.getElementById('inTopic').value.trim(),
        url: document.getElementById('inUrl').value.trim(),
      })
    });
    const d = await r.json();
    if (d.ok) {
      document.getElementById('inText').value = '';
      document.getElementById('inUrl').value = '';
      const msg = document.getElementById('addMsg');
      msg.style.display = 'block';
      setTimeout(() => msg.style.display = 'none', 3000);
      loadStats();
    }
  } catch(e) { alert('失败: '+e.message); }
}

async function loadItems() {
  try {
    const r = await fetch('/api/inbox'); const d = await r.json();
    const el = document.getElementById('itemList');
    if (!d.items.length) { el.innerHTML = '<div style="color:#aaa;text-align:center">还没有材料</div>'; return; }
    el.innerHTML = d.items.slice().reverse().map(i => `<div class="item">
      <span class="status status-${i.status}">${i.status}</span>
      <strong>#${i.id}</strong> ${i.text.substring(0,120)}${i.text.length>120?'...':''}
      <div class="meta">${i.source?i.source+' · ':''}${i.topic?i.topic+' · ':''}${i.added_at}</div>
    </div>`).join('');
  } catch(e) { document.getElementById('itemList').textContent = '加载失败'; }
}

async function loadDrafts() {
  try {
    const r = await fetch('/api/inbox/drafts'); const d = await r.json();
    const el = document.getElementById('draftList');
    if (!d.drafts.length) { el.innerHTML = '<div style="color:#aaa;text-align:center">还没有草稿</div>'; return; }
    el.innerHTML = d.drafts.map(f => `<div class="draft">${f.content}<div class="meta">${f.filename}</div></div>`).join('');
  } catch(e) { document.getElementById('draftList').textContent = '加载失败'; }
}

loadStats();
</script>
</body></html>"""

@app.route("/inbox")
def inbox_page():
    page = INBOX_PAGE.replace("书先生", AI_NAME)
    return Response(page, content_type="text/html; charset=utf-8")


# ══════════════════════════════════════
#  Voice Chat Page — /talk
# ══════════════════════════════════════

VOICE_CHAT_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>书先生 · 语音</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600&display=swap');

  :root {
    --bg: #1a2420;
    --bg-light: #243530;
    --accent: #6B8F71;
    --accent-warm: #D4956A;
    --text: #e0ebe5;
    --text-dim: #8a9b94;
    --red: #e74c3c;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    height: 100vh; height: 100dvh;
    background: var(--bg);
    font-family: 'Noto Serif SC', 'PingFang SC', serif;
    color: var(--text);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    -webkit-user-select: none;
    user-select: none;
  }

  .top-bar {
    position: fixed;
    top: 0; left: 0; right: 0;
    padding: 50px 20px 16px;
    text-align: center;
  }
  .top-bar h1 {
    font-size: 1.3rem;
    font-weight: 600;
    letter-spacing: 0.15em;
    color: var(--text);
  }
  .top-bar .subtitle {
    font-size: 0.8rem;
    color: var(--text-dim);
    margin-top: 4px;
  }
  .back-link {
    position: fixed;
    top: 52px; left: 16px;
    color: var(--text-dim);
    text-decoration: none;
    font-size: 0.9rem;
  }
  .back-link:hover { color: var(--text); }

  /* ─── Main Circle ─── */
  .circle-wrap {
    position: relative;
    width: 200px; height: 200px;
    margin-bottom: 40px;
  }

  .circle-bg {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    background: var(--bg-light);
    border: 2px solid rgba(107, 143, 113, 0.3);
    transition: all 0.4s;
  }

  .circle-pulse {
    position: absolute;
    inset: -20px;
    border-radius: 50%;
    background: rgba(107, 143, 113, 0.15);
    opacity: 0;
    transition: opacity 0.3s;
  }

  .circle-icon {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 3rem;
    z-index: 2;
  }

  /* States */
  body.state-idle .circle-bg { cursor: pointer; }
  body.state-idle .circle-bg:hover { border-color: var(--accent); background: #2a4538; }

  body.state-listening .circle-bg {
    border-color: var(--red);
    background: rgba(231, 76, 60, 0.15);
    cursor: pointer;
  }
  body.state-listening .circle-pulse {
    opacity: 1;
    background: rgba(231, 76, 60, 0.08);
    animation: pulse 1.5s ease-in-out infinite;
  }

  body.state-thinking .circle-bg {
    border-color: var(--accent-warm);
    background: rgba(212, 149, 106, 0.1);
  }
  body.state-thinking .circle-icon {
    animation: spin 2s linear infinite;
  }

  body.state-speaking .circle-bg {
    border-color: var(--accent);
    background: rgba(107, 143, 113, 0.15);
  }
  body.state-speaking .circle-pulse {
    opacity: 1;
    background: rgba(107, 143, 113, 0.1);
    animation: pulse 1s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 0.6; }
    50% { transform: scale(1.15); opacity: 0; }
  }
  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  /* ─── Status Text ─── */
  .status {
    font-size: 0.95rem;
    color: var(--text-dim);
    text-align: center;
    min-height: 1.4em;
    margin-bottom: 12px;
  }
  .transcript {
    font-size: 0.85rem;
    color: var(--text-dim);
    text-align: center;
    max-width: 280px;
    min-height: 1.2em;
    opacity: 0.7;
  }

  /* ─── Bottom Controls ─── */
  .bottom-bar {
    position: fixed;
    bottom: 40px;
    display: flex;
    gap: 40px;
    align-items: center;
  }

  .ctrl-btn {
    width: 56px; height: 56px;
    border-radius: 50%;
    border: none;
    font-size: 1.3rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
  }
  .ctrl-btn:active { transform: scale(0.92); }

  .btn-end {
    background: var(--red);
    color: #fff;
  }
  .btn-chat {
    background: var(--bg-light);
    color: var(--text-dim);
    border: 1px solid rgba(107, 143, 113, 0.3);
  }
  .btn-chat:hover { color: var(--text); border-color: var(--accent); }

  /* ─── Timer ─── */
  .timer {
    font-size: 0.8rem;
    color: var(--red);
    font-variant-numeric: tabular-nums;
    min-height: 1.2em;
    margin-top: 8px;
  }
</style>
</head>
<body class="state-idle">

<a href="/" class="back-link">← 返回</a>

<div class="top-bar">
  <h1>书 先 生</h1>
  <div class="subtitle">语音通话</div>
</div>

<div class="circle-wrap" onclick="onCircleTap()">
  <div class="circle-pulse"></div>
  <div class="circle-bg"></div>
  <div class="circle-icon" id="circleIcon">🎙️</div>
</div>

<div class="status" id="statusText">轻触开始说话</div>
<div class="transcript" id="transcript"></div>
<div class="timer" id="timer"></div>

<div class="bottom-bar">
  <a href="/"><button class="ctrl-btn btn-chat" title="返回文字">💬</button></a>
  <button class="ctrl-btn btn-end" onclick="endVoiceChat()" title="结束">✕</button>
</div>

<script>
let state = 'idle'; // idle, listening, thinking, speaking
let mediaRecorder = null;
let audioChunks = [];
let recStartTime = 0;
let timerInterval = null;
let continueLoop = true;

// iOS 需要在用户手势中解锁 Audio 元素，之后才能程序化播放
// 创建一个持久的 Audio 元素，在第一次点击时解锁
const shuAudio = new Audio();
let audioUnlocked = false;

function unlockAudio() {
  if (audioUnlocked) return;
  // 播放一个极短的静音来解锁
  shuAudio.src = 'data:audio/mp3;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYYoRwAAAAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYYoRwAAAAAAAAAAAAAAAAAAAA==';
  shuAudio.play().then(() => {
    audioUnlocked = true;
    shuAudio.pause();
  }).catch(() => {});
}

function setState(s) {
  state = s;
  document.body.className = 'state-' + s;
  const icon = document.getElementById('circleIcon');
  const status = document.getElementById('statusText');
  const timer = document.getElementById('timer');

  if (s === 'idle') {
    icon.textContent = '🎙️';
    status.textContent = '轻触开始说话';
    timer.textContent = '';
  } else if (s === 'listening') {
    icon.textContent = '🔴';
    status.textContent = '正在听…';
  } else if (s === 'thinking') {
    icon.textContent = '⏳';
    status.textContent = '书先生在想…';
    timer.textContent = '';
  } else if (s === 'speaking') {
    icon.textContent = '🔊';
    status.textContent = '书先生在说…';
  }
}

function onCircleTap() {
  unlockAudio();
  if (state === 'idle') {
    continueLoop = true;
    startRecording();
  } else if (state === 'listening') {
    stopRecording();
  }
}

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : (MediaRecorder.isTypeSupported('audio/mp4') ? 'audio/mp4' : '');
    mediaRecorder = mimeType
      ? new MediaRecorder(stream, { mimeType })
      : new MediaRecorder(stream);

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = () => {
      clearInterval(timerInterval);
      stream.getTracks().forEach(t => t.stop());
      if (audioChunks.length > 0) {
        sendAndPlay(new Blob(audioChunks));
      } else {
        setState('idle');
      }
    };

    mediaRecorder.start();
    setState('listening');
    recStartTime = Date.now();
    timerInterval = setInterval(updateTimer, 200);

    // Auto stop after 30s
    setTimeout(() => {
      if (state === 'listening' && mediaRecorder && mediaRecorder.state === 'recording') {
        stopRecording();
      }
    }, 30000);
  } catch (e) {
    alert('无法访问麦克风：' + e.message);
    setState('idle');
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
  }
}

function updateTimer() {
  const elapsed = Math.floor((Date.now() - recStartTime) / 1000);
  const m = String(Math.floor(elapsed / 60)).padStart(1, '0');
  const s = String(elapsed % 60).padStart(2, '0');
  document.getElementById('timer').textContent = m + ':' + s;
}

async function sendAndPlay(blob) {
  setState('thinking');
  document.getElementById('transcript').textContent = '';
  try {
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 8192) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    }
    const b64 = btoa(binary);

    const res = await fetch('/api/voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio_base64: b64 }),
    });

    const contentType = res.headers.get('content-type') || '';

    if (contentType.includes('audio')) {
      setState('speaking');
      const audioBlob = await res.blob();
      const url = URL.createObjectURL(audioBlob);

      // 用持久的 Audio 元素播放（iOS 兼容）
      shuAudio.onended = () => {
        URL.revokeObjectURL(url);
        shuAudio.onended = null;
        shuAudio.onerror = null;
        if (continueLoop) {
          startRecording();
        } else {
          setState('idle');
        }
      };

      shuAudio.onerror = () => {
        URL.revokeObjectURL(url);
        shuAudio.onended = null;
        shuAudio.onerror = null;
        if (continueLoop) startRecording();
        else setState('idle');
      };

      shuAudio.src = url;
      shuAudio.play().catch(() => {
        // 如果还是播放失败，显示文字提示
        URL.revokeObjectURL(url);
        document.getElementById('transcript').textContent = '播放失败，轻触重试';
        setState('idle');
      });
    } else {
      // JSON response (silence / deferred)
      const data = await res.json();
      if (data.reply) {
        document.getElementById('transcript').textContent = data.reply;
      }
      // 短暂停顿后继续
      if (continueLoop) {
        setTimeout(() => startRecording(), 1500);
      } else {
        setState('idle');
      }
    }
  } catch (e) {
    document.getElementById('transcript').textContent = '连接失败，请重试';
    setState('idle');
  }
}

function endVoiceChat() {
  continueLoop = false;
  if (state === 'listening') {
    stopRecording();
  }
  setState('idle');
  shuAudio.pause();
  shuAudio.src = '';
  shuAudio.onended = null;
  shuAudio.onerror = null;
}

// 额外的触摸解锁
document.addEventListener('touchstart', () => unlockAudio(), { once: true });
</script>

</body>
</html>"""


@app.route("/talk")
def talk_page():
    page = VOICE_CHAT_PAGE.replace("书先生", AI_NAME).replace("书 先 生", " ".join(AI_NAME))
    return Response(page, content_type="text/html; charset=utf-8")


# ══════════════════════════════════════
#  Main
# ══════════════════════════════════════

if __name__ == "__main__":
    port = 5210
    print(f"\n  {AI_NAME}")
    print(f"  打开浏览器访问: http://localhost:{port}")
    print(f"  按 Ctrl+C 停止\n")
    app.run(host="0.0.0.0", port=port, debug=False)
