#!/usr/bin/env python3
"""
sync_notion.py — Sync a markdown file to a Notion page.

Usage:
    python scripts/sync_notion.py --file PENDING.md --page <page_id>

Requires:
    NOTION_TOKEN env var — internal Notion integration token
    pip install requests
"""

import argparse
import os
import re
import sys
import time
import requests

NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"
MAX_TEXT_LEN = 2000  # Notion rich_text content limit per block


def _headers():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("Error: NOTION_TOKEN environment variable is not set.")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _rich_text(content: str) -> list:
    """Convert a plain string to Notion rich_text array, splitting on code spans."""
    if not content:
        return [{"type": "text", "text": {"content": ""}}]
    result = []
    # Split on `backtick spans`
    parts = re.split(r'(`[^`]+`)', content)
    for part in parts:
        if part.startswith('`') and part.endswith('`') and len(part) > 1:
            result.append({
                "type": "text",
                "text": {"content": part[1:-1][:MAX_TEXT_LEN]},
                "annotations": {"code": True},
            })
        else:
            # Handle **bold**
            bold_parts = re.split(r'(\*\*[^*]+\*\*)', part)
            for bp in bold_parts:
                if bp.startswith('**') and bp.endswith('**') and len(bp) > 4:
                    result.append({
                        "type": "text",
                        "text": {"content": bp[2:-2][:MAX_TEXT_LEN]},
                        "annotations": {"bold": True},
                    })
                elif bp:
                    result.append({
                        "type": "text",
                        "text": {"content": bp[:MAX_TEXT_LEN]},
                    })
    return result if result else [{"type": "text", "text": {"content": ""}}]


def md_to_blocks(md: str) -> list:
    """Convert markdown string to a list of Notion block dicts."""
    blocks = []
    lines = md.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            lang = line[3:].strip() or "plain text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_content = "\n".join(code_lines)[:2000]
            blocks.append({
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": code_content}}],
                    "language": lang,
                },
            })
            i += 1
            continue

        # Divider
        if re.match(r'^-{3,}$', line.strip()):
            blocks.append({"type": "divider", "divider": {}})
            i += 1
            continue

        # Headings
        if line.startswith("#### "):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": _rich_text(line[5:])}})
            i += 1
            continue
        if line.startswith("### "):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": _rich_text(line[4:])}})
            i += 1
            continue
        if line.startswith("## "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": _rich_text(line[3:])}})
            i += 1
            continue
        if line.startswith("# "):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": _rich_text(line[2:])}})
            i += 1
            continue

        # Blockquote
        if line.startswith("> "):
            blocks.append({"type": "quote", "quote": {"rich_text": _rich_text(line[2:])}})
            i += 1
            continue

        # Checkbox (todo)
        todo_m = re.match(r'^[-*]\s+\[([ xX])\]\s+(.*)', line)
        if todo_m:
            checked = todo_m.group(1).lower() == 'x'
            blocks.append({
                "type": "to_do",
                "to_do": {"rich_text": _rich_text(todo_m.group(2)), "checked": checked},
            })
            i += 1
            continue

        # Bullet list
        if re.match(r'^[-*]\s+', line):
            text = re.sub(r'^[-*]\s+', '', line)
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(text)},
            })
            i += 1
            continue

        # Numbered list
        num_m = re.match(r'^\d+\.\s+(.*)', line)
        if num_m:
            blocks.append({
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _rich_text(num_m.group(1))},
            })
            i += 1
            continue

        # Table row — render cells as a paragraph (Notion table blocks are complex)
        if line.startswith("|"):
            # Skip markdown separator rows like |---|---|
            if re.match(r'^\|[-|\s:]+\|$', line):
                i += 1
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            text = "  │  ".join(cells)
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}})
            i += 1
            continue

        # Plain paragraph (non-empty)
        if line.strip():
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": _rich_text(line)}})

        i += 1

    return blocks


def _get_child_block_ids(page_id: str) -> list:
    url = f"{BASE_URL}/blocks/{page_id}/children?page_size=100"
    ids = []
    while url:
        resp = requests.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        ids.extend(b["id"] for b in data.get("results", []))
        url = data.get("next_cursor") and f"{BASE_URL}/blocks/{page_id}/children?page_size=100&start_cursor={data['next_cursor']}"
    return ids


def _delete_blocks(block_ids: list):
    for bid in block_ids:
        r = requests.delete(f"{BASE_URL}/blocks/{bid}", headers=_headers())
        if r.status_code not in (200, 404):
            print(f"  Warning: delete {bid} returned {r.status_code}")
        time.sleep(0.1)  # stay under Notion rate limit (3 req/s)


def _append_blocks(page_id: str, blocks: list):
    url = f"{BASE_URL}/blocks/{page_id}/children"
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        resp = requests.patch(url, headers=_headers(), json={"children": chunk})
        if not resp.ok:
            print(f"  Error appending chunk {i//100 + 1}: {resp.status_code} {resp.text[:300]}")
            resp.raise_for_status()
        time.sleep(0.3)


def sync(filepath: str, page_id: str):
    from datetime import datetime, timezone
    page_id = page_id.replace("-", "")

    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    print(f"→ Converting {filepath} ({len(content)} chars) to Notion blocks...")
    blocks = md_to_blocks(content)
    print(f"  {len(blocks)} blocks")

    # Prepend a sync timestamp notice
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    notice = {
        "type": "callout",
        "callout": {
            "rich_text": _rich_text(f"Auto-synced from {filepath} at {ts}. Do not edit — next push overwrites."),
            "icon": {"emoji": "🔄"},
        },
    }
    blocks = [notice] + blocks

    print(f"→ Clearing existing blocks on page {page_id}...")
    existing = _get_child_block_ids(page_id)
    print(f"  Deleting {len(existing)} blocks...")
    _delete_blocks(existing)

    print(f"→ Writing {len(blocks)} blocks...")
    _append_blocks(page_id, blocks)
    print(f"✅ Done: {filepath} → {page_id}")


def main():
    ap = argparse.ArgumentParser(description="Sync a markdown file to a Notion page.")
    ap.add_argument("--file", required=True, help="Path to the markdown file")
    ap.add_argument("--page", required=True, help="Notion page ID (with or without dashes)")
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        sys.exit(f"Error: file not found: {args.file}")

    sync(args.file, args.page)


if __name__ == "__main__":
    main()
