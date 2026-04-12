---
title: "feat: Return structured sources and metadata from /chat"
type: feat
status: completed
date: 2026-04-11
---

# feat: Return structured sources and metadata from /chat

## Overview

The grok-bridge `/chat` endpoint currently returns only cleaned text. Grok's responses include inline links (X post URLs, web article URLs) and a source count, but these get flattened into plain text or stripped entirely. This change extracts source links from the DOM alongside the text response, and includes the original query in the response, so every answer is fully traceable back to its sources.

## Problem Frame

User asks Grok to search X. Grok returns a summary citing @username posts with source labels. But grok-bridge's `document.body.innerText` extraction loses all `<a href>` data. The user gets a summary they can't verify. Reproducibility requires: original query, source URLs, timestamps, and the response text together in one JSON payload.

## Scope Boundaries

- Only extract links from the response area, not navigation/sidebar/history
- Do not attempt to parse or structure individual source metadata (title, date, author). Just return the raw URL list
- Do not change the existing `response` text field format
- Do not add pagination or source filtering

## Key Technical Decisions

- **DOM link extraction via `page.evaluate()`**: Extract all `<a href>` elements from the response area after polling completes, filter out grok.com internal links and cookie banners. This is the same mechanism we already use for `innerText`
- **Add fields to existing JSON response**: New fields are additive, no breaking change. Existing consumers that only read `status`/`response`/`elapsed` are unaffected
- **Source count from innerText**: Parse the "N sources" text that appears at the bottom of Grok responses rather than counting DOM elements (more reliable, matches what the user sees)
- **Query echo**: Include the original prompt in the response for traceability

## Implementation Units

- [ ] **Unit 1: Extract sources from DOM after response stabilizes**

**Goal:** After `_poll_response` detects a stable response, extract all external links from the page and parse the source count.

**Files:**
- Modify: `grok_bridge.py` (GrokBridge._poll_response, new _extract_sources method)

**Approach:**
- Add `_extract_sources()` method that runs `page.evaluate()` with JS to:
  1. Collect all `<a href>` where href starts with `http` and doesn't include `grok.com`, `cookiepedia`, `onetrust`
  2. Return array of `{url, text}` objects
  3. Parse `innerText` for the `/(\d+)\s*sources?/i` pattern to get source count
- Call `_extract_sources()` at the end of `_poll_response`, after the text extraction
- Deduplicate URLs (same href appearing multiple times)

**Patterns to follow:**
- Existing `_poll_response` method structure
- Existing `page.evaluate()` usage in `_extract` and `history`

**Test scenarios:**
- Happy path: response with inline URLs returns sources array with correct hrefs
- Happy path: response with "48 sources" text returns source_count=48
- Edge case: response with no sources returns empty array and source_count=0
- Edge case: duplicate URLs are deduplicated

**Verification:**
- Send a search query via curl, response JSON contains `sources` array with real URLs

- [ ] **Unit 2: Add query echo and metadata to /chat response**

**Goal:** Include original prompt, mode, and timestamp in the response JSON.

**Files:**
- Modify: `grok_bridge.py` (GrokBridge.chat, _Handler._handle_chat)

**Approach:**
- `chat()` returns additional fields: `query` (original prompt), `mode` (which mode was used or null), `sources` (from Unit 1), `source_count` (integer)
- Response format becomes:
  ```json
  {
    "status": "ok",
    "response": "...",
    "elapsed": 15.2,
    "query": "search X for ...",
    "mode": "expert",
    "sources": [
      {"url": "https://x.com/user/status/123", "text": "Username"},
      {"url": "https://techcrunch.com/...", "text": "Techcrunch"}
    ],
    "source_count": 48
  }
  ```

**Patterns to follow:**
- Existing response dict construction in `chat()` and `_poll_response`

**Test scenarios:**
- Happy path: response includes query field matching the sent prompt
- Happy path: response includes mode field when mode was specified
- Happy path: response includes mode=null when no mode specified
- Edge case: timeout response also includes sources and query

**Verification:**
- curl POST /chat with mode=expert, verify response JSON has all new fields
- curl POST /chat without mode, verify mode is null
