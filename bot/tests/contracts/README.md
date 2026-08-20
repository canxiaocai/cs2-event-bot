# Pending command and configuration contracts

These cases define security and configuration boundaries. The URL policy and current Pydantic
bounds are executable in `../test_security_and_config.py`; the remaining permission/cooldown
matrix stays here until its command-policy function is extracted. Tests must not import the plugin
package because importing it registers jobs and handlers.

## Debug match URL

The pure function has this interface:

```python
def hltv_match_url(raw: str) -> str | None: ...
```

Acceptance table:

| Input | Result |
|---|---|
| `https://www.hltv.org/matches/123/a-vs-b` | normalized URL |
| surrounding whitespace | stripped normalized URL |
| `http://www.hltv.org/matches/123/a-vs-b` | reject |
| `https://hltv.org/matches/123/a-vs-b` | reject unless explicitly canonicalized |
| `https://www.hltv.org.evil.test/matches/123/a` | reject |
| `https://www.hltv.org@127.0.0.1/matches/123/a` | reject |
| `https://www.hltv.org:444/matches/123/a` | reject |
| `https://www.hltv.org/events/123/a` | reject |
| non-numeric match id, fragment, credentials or control characters | reject |

The fetcher must validate the final URL after every redirect with the same policy. DNS/IP
checks are defense in depth; exact scheme, host, port and path checks remain mandatory.

## Configuration boundaries

Move validation into `Config` so invalid values fail during startup rather than inside a job:

- Poll/scan/warm intervals, caps, cache TTLs and keep-days are non-negative integers;
  enabled recurring intervals are at least one minute.
- `cs2_request_min_gap >= 0`, `1_000 <= cs2_nav_timeout <= 120_000`, and
  `0 <= cs2_challenge_retries <= 10`.
- Group ids are unique positive integers; event ids contain digits only.
- The effective event warm interval is not shorter than the request queue can service and
  should not be shorter than `ceil(cs2_cache_event_page_ttl / 60)` without an explicit reason.
- Environment subscriptions are migration seeds, not a second permanent source of truth.

The implemented URL and numeric/cache-boundary rows are unit tested. Once command policy is a
pure function, also test debug-group member, superuser private-message, ordinary group and ordinary
private-message permission matrices, including cooldown identity and expiration.
