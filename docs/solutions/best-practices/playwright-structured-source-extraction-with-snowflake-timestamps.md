---
title: Structured source extraction with snowflake timestamps in Playwright browser bridges
date: 2026-04-11
category: best-practices
module: grok-bridge
problem_type: best_practice
component: tooling
severity: medium
applies_when:
  - Building a browser automation bridge that returns search results to downstream consumers
  - Extracting structured metadata (URLs, timestamps, authors) from DOM content
  - Working with X/Twitter post URLs that contain snowflake IDs
tags:
  - playwright
  - browser-automation
  - source-extraction
  - snowflake-id
  - x-twitter
  - grok-bridge
  - dom-scraping
---

# Structured source extraction with snowflake timestamps in Playwright browser bridges

## Context

When building a browser automation bridge (like grok-bridge, which proxies Grok queries via Playwright + Chrome), the raw text response alone is not enough for downstream consumers. Summaries need verifiable source links so users can trace claims back to original posts. Without structured source metadata, consumers get summaries they cannot verify, which undermines trust and usefulness.

The specific friction: grok-bridge returned Grok's answer text but no structured source data. Users had to manually copy-paste URLs from the browser. Additionally, X/Twitter post URLs contain snowflake IDs that encode the exact creation timestamp, but this metadata was not being surfaced.

## Guidance

### 1. Extract links from the DOM, not from response text

Use `page.evaluate()` to pull all `<a>` tags from Grok's source section rather than regex-parsing the response body. DOM extraction is more reliable because the rendered page has clean `href` attributes while the text representation may mangle or omit URLs.

```python
def _extract_sources(self, body: str) -> tuple[list[dict], int]:
    raw_links = self._page.evaluate("""() => {
        const links = document.querySelectorAll('a[href]');
        return Array.from(links)
            .filter(a => a.href.startsWith('http') && !a.href.includes('grok.com'))
            .map(a => ({url: a.href, text: (a.textContent || '').trim()}));
    }""")
    # ... deduplicate and enrich
```

### 2. Clean referrer params and deduplicate

Grok appends `?referrer=grok-com` to outbound links. Strip it for cleaner URLs, then deduplicate by the cleaned URL:

```python
seen = set()
for link in raw_links:
    clean_url = re.sub(r"\?referrer=grok-com$", "", link["url"])
    if clean_url in seen:
        continue
    seen.add(clean_url)
    entry = {"url": clean_url, "text": link["text"]}
    sources.append(entry)
```

### 3. Extract timestamps from X snowflake IDs

X/Twitter status IDs are snowflake IDs encoding the creation timestamp. The formula: shift right 22 bits, add the Twitter epoch (1288834974657 ms). This gives you the exact post time without any API call.

```python
_X_STATUS_RE = re.compile(r"x\.com/.+/status/(\d+)")

@staticmethod
def _snowflake_to_iso(status_id: str) -> str | None:
    try:
        sid = int(status_id)
        ts_ms = (sid >> 22) + 1288834974657
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError):
        return None
```

Then enrich each source entry:

```python
m = self._X_STATUS_RE.search(clean_url)
if m:
    ts = self._snowflake_to_iso(m.group(1))
    if ts:
        entry["timestamp"] = ts
```

### 4. Return structured JSON, not just text

The `/chat` endpoint returns sources alongside the response:

```json
{
  "status": "ok",
  "response": "Grok's answer text...",
  "sources": [
    {"url": "https://x.com/user/status/123", "text": "Post text", "timestamp": "2026-04-10T14:32:00Z"},
    {"url": "https://example.com/article", "text": "Article title"}
  ],
  "source_count": 5,
  "elapsed": 12.3
}
```

Non-X sources get `url` and `text` only. X sources additionally get `timestamp`. Consumers can format this however they need.

## Why This Matters

**Verifiability.** AI-generated summaries are only as good as their traceability. When a consumer (human or agent) can click through to the original post and see the exact timestamp, they can assess recency and credibility. Without source links, every summary is an unverifiable claim.

**Zero-cost metadata.** Snowflake timestamp extraction requires no API calls, no rate limits, no auth tokens. It's pure math on data already present in the URL. There is no reason not to include it.

**Downstream flexibility.** Structured JSON lets consumers choose their own display format: inline citations, source tables, filtered-by-date views. Raw text locks everyone into one format.

## When to Apply

- Building any browser automation bridge that returns search results or citations
- Working with X/Twitter URLs in any scraping or aggregation context
- Designing REST APIs that wrap AI chat interfaces (Grok, ChatGPT, Perplexity, etc.)
- Any system where downstream consumers need to verify AI-generated claims

## Examples

### Before: text-only response

```json
{"status": "ok", "response": "Several users discussed...", "elapsed": 15.2}
```

Consumer has no way to verify claims or find original posts.

### After: structured sources with timestamps

```json
{
  "status": "ok",
  "response": "Several users discussed...",
  "sources": [
    {
      "url": "https://x.com/devuser/status/1910678234567890123",
      "text": "@devuser: nerd-dictation works great for...",
      "timestamp": "2026-04-09T08:15:23Z"
    },
    {
      "url": "https://x.com/mleng/status/1910543210987654321",
      "text": "@mleng: sherpa-onnx has the best latency...",
      "timestamp": "2026-04-08T22:41:07Z"
    },
    {
      "url": "https://github.com/some/repo",
      "text": "GitHub repository"
    }
  ],
  "source_count": 12,
  "elapsed": 15.2
}
```

Consumer can render source tables, sort by time, filter by author, or inline-cite.

## Related

- `docs/plans/2026-04-11-001-feat-structured-sources-plan.md` (implementation plan for this feature)
