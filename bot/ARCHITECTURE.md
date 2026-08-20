# Bot architecture

NapCat is the QQ/OneBot gateway. NoneBot owns all business behavior. New `/cs2` features live
inside `src/plugins/cs2_results` and should preserve the dependency direction below:

```text
commands/jobs -> services -> domain models
                         -> repository/delivery/fetcher ports
adapters (HLTV, SQLite, OneBot, Playwright) -> those ports
renderers -> domain view models + resolved assets
```

`__init__.py` is the composition root: register handlers/jobs and wire dependencies only. A
handler parses input, checks permission/cooldown, calls one service, then maps the service result
to a reply. Services do not call `finish()`, read NoneBot event objects, or render HTML. Parsers
accept HTML and return domain data without network, storage, clock or bot access. Renderers accept
models and assets without reading global storage.

## Operational contracts

- One fetch worker owns HLTV rate limiting and prioritization. Jobs enqueue work; they do not
  compete on independent sleeps or locks.
- Delivery is an outbox keyed by `(match, map, group)`. Only a successful OneBot response marks
  a row sent; transient errors retain retry state with bounded exponential backoff. The rendered
  PNG is persisted per batch, so retries do not depend on HLTV, rendering, or live-match tracking.
- Match tracking is a producer: it renders each report once and enqueues it. A separately managed,
  leased outbox consumer sends to QQ, survives restarts, exposes dead-letter replay, honors
  unsubscribe immediately, and retains unresolved dead letters for operators.
- Durable changes use migrations and transactions. Schema changes include forward migration,
  compatibility/rollback notes and a backup strategy.
- Runtime subscriptions have one source of truth. Environment values may seed an empty store once.
- Every background task is named, tracked, cancelable and awaited during shutdown.
- External state uses typed models/enums. Metrics/logs include operation, match/event/group ids,
  latency and outcome, but never secrets, cookies or full message payloads.

## Test seams

Keep imports side-effect free below the command/job layer. Inject the clock, fetch port, repository,
delivery adapter and renderer into services. This allows parser fixtures, fake delivery failures,
queue scheduling tests and migration tests without starting NoneBot, Playwright or NapCat.

See [FEATURE_CHECKLIST.md](FEATURE_CHECKLIST.md) for the definition of done and
[tests/contracts/README.md](tests/contracts/README.md) for command/security/configuration contracts.
