"""
feed.py — 往书先生的材料池里喂东西。

两种用法：
  1. 截图丢进 feed_inbox 文件夹，双击 run_feed.bat
  2. 文本文件丢进 feed_inbox 文件夹，双击 run_feed.bat

截图用法（最方便）：
  1. 小红书帖子截图，丢进 feed_inbox/
  2.（可选）放一个 meta.txt 写元信息：
       PLATFORM: 小红书
       TOPIC: 本地美食
       CONTEXT: 书先生可能感兴趣
  3. 双击 run_feed.bat
  4. 截图会被 Claude 识别文字，自动放进材料池

同一批截图会合并成一条材料（按文件名排序）。
适合一篇帖子截了多张图的情况。

文本文件用法：
  PLATFORM: 小红书
  TOPIC: 本地咖啡
  CONTEXT: 书先生可能想看
  ---
  （帖子正文粘贴在下面）

用法：
  python feed.py                 导入 feed_inbox/ 里所有文件
  python feed.py somefile.png    导入指定文件
"""

import os
import sys
import json
import base64
import shutil
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent
FEED_INBOX = BASE_DIR / "feed_inbox"
FEED_ARCHIVE = FEED_INBOX / "_archived"
INBOX_FILE = BASE_DIR / "inbox.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


# ══════════════════════════════════════
#  Inbox — 直接读写 inbox.json
# ══════════════════════════════════════

def load_inbox():
    try:
        if INBOX_FILE.exists():
            return json.loads(INBOX_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def save_inbox(items):
    INBOX_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def add_to_inbox(text, source="", topic="", url="", context=""):
    items = load_inbox()
    item = {
        "id": len(items) + 1,
        "text": text,
        "url": url,
        "source": source,
        "topic": topic,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "unread",
    }
    if context:
        item["context"] = context
    items.append(item)
    save_inbox(items)
    return item


# ══════════════════════════════════════
#  Claude Vision: 截图识别文字
# ══════════════════════════════════════

def extract_text_from_image(image_path: Path) -> str:
    """用 Claude 识别截图中的文字。"""
    api_key = os.getenv("CLAUDE_API_KEY", "")
    if not api_key:
        raise ValueError("CLAUDE_API_KEY 没有设置。检查 run_feed.bat")

    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    suffix = image_path.suffix.lower()
    media_type = {
        ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".webp": "image/webp"
    }.get(suffix, "image/png")

    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    }
                },
                {
                    "type": "text",
                    "text": (
                        "这是一张社交媒体帖子的截图。请提取截图中的所有文字内容，"
                        "按原始顺序输出。只输出文字内容，不要加任何解释、总结或判断。"
                        "如果有多段文字，保持原来的分段。"
                        "如果截图中有作者名、点赞数、评论数等元信息，也一并提取。"
                    )
                },
            ]
        }]
    )

    return response.content[0].text.strip()


# ══════════════════════════════════════
#  解析元信息
# ══════════════════════════════════════

def parse_meta_file(filepath: Path) -> dict:
    text = filepath.read_text(encoding="utf-8").strip()
    header = {}
    for line in text.splitlines():
        line = line.strip()
        if line == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            header[key.strip().lower()] = value.strip()
    return header


def parse_text_file(text: str) -> dict:
    if "---" in text:
        parts = text.split("---", 1)
        header_text = parts[0].strip()
        body = parts[1].strip()
    else:
        header_text = ""
        body = text.strip()

    header = {}
    if header_text:
        for line in header_text.splitlines():
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                header[key.strip().lower()] = value.strip()

    return {
        "text": body,
        "source": header.get("platform", ""),
        "topic": header.get("topic", ""),
        "url": header.get("url", ""),
        "context": header.get("context", ""),
    }


# ══════════════════════════════════════
#  喂文本文件
# ══════════════════════════════════════

def feed_text_file(filepath: Path) -> bool:
    text = filepath.read_text(encoding="utf-8")
    if not text.strip():
        print(f"  跳过（空文件）：{filepath.name}")
        return False

    data = parse_text_file(text)
    if not data["text"]:
        print(f"  跳过（没有正文）：{filepath.name}")
        return False

    item = add_to_inbox(**data)
    print(f"  ✓ 文本导入：{filepath.name} → #{item['id']}")
    return True


# ══════════════════════════════════════
#  喂截图（Claude 识别）
# ══════════════════════════════════════

def feed_images(image_files: list, meta: dict = None) -> bool:
    if not image_files:
        return False

    meta = meta or {}
    all_text_parts = []

    for img in image_files:
        print(f"  识别截图：{img.name} ...")
        try:
            text = extract_text_from_image(img)
            if text:
                all_text_parts.append(text)
                print(f"    提取了 {len(text)} 个字")
            else:
                print(f"    没有识别到文字")
        except Exception as e:
            print(f"    出错：{e}")

    if not all_text_parts:
        print("  所有截图都没有识别到文字。")
        return False

    combined_text = "\n\n".join(all_text_parts)

    item = add_to_inbox(
        text=combined_text,
        source=meta.get("platform", ""),
        topic=meta.get("topic", ""),
        url=meta.get("url", ""),
        context=meta.get("context", ""),
    )
    print(f"  ✓ {len(image_files)} 张截图 → #{item['id']}（{len(combined_text)} 字）")
    return True


# ══════════════════════════════════════
#  主逻辑
# ══════════════════════════════════════

def feed_all():
    inbox = FEED_INBOX
    archive = FEED_ARCHIVE
    archive.mkdir(parents=True, exist_ok=True)

    all_files = sorted(f for f in inbox.iterdir()
                       if f.is_file() and not f.name.startswith("_"))

    txt_files = [f for f in all_files
                 if f.suffix == ".txt" and f.stem != "meta"]
    image_files = [f for f in all_files
                   if f.suffix.lower() in IMAGE_EXTENSIONS]
    meta_file = inbox / "meta.txt"

    count = 0

    # 1. 文本文件（每个独立一条）
    for f in txt_files:
        ok = feed_text_file(f)
        if ok:
            count += 1
            shutil.move(str(f), str(archive / f.name))

    # 2. 截图（合并成一条）
    if image_files:
        meta = {}
        if meta_file.exists():
            meta = parse_meta_file(meta_file)
            print(f"  使用 meta.txt 中的元信息")

        ok = feed_images(image_files, meta)
        if ok:
            count += 1
            for f in image_files:
                shutil.move(str(f), str(archive / f.name))
            if meta_file.exists():
                shutil.move(str(meta_file), str(archive / f"meta_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"))

    if not txt_files and not image_files:
        print(f"feed_inbox/ 里没有文件。")
        print(f"把截图或 .txt 文件丢进去再运行。")

    print(f"\n完成！导入了 {count} 条材料。")


def main():
    FEED_INBOX.mkdir(parents=True, exist_ok=True)
    FEED_ARCHIVE.mkdir(parents=True, exist_ok=True)

    args = sys.argv[1:]

    if not args:
        feed_all()
    elif args[0] in ("--help", "-h"):
        print(__doc__)
    else:
        for path_str in args:
            p = Path(path_str)
            if not p.exists():
                p = FEED_INBOX / path_str
            if not p.exists():
                print(f"找不到文件：{path_str}")
                continue
            if p.suffix.lower() in IMAGE_EXTENSIONS:
                feed_images([p])
            elif p.suffix == ".txt":
                feed_text_file(p)
            else:
                print(f"不支持的文件类型：{p.suffix}")


if __name__ == "__main__":
    main()
