# `/cs2` feature definition of done

Use this checklist before enabling a feature in production.

## Product and command surface

- [ ] Define user outcome, command syntax, empty/error copy and backward compatibility.
- [ ] Document the permission matrix for group member, group admin, debug group and superuser.
- [ ] Set per-user/per-group cooldown plus global concurrency; admin bypass is explicit and audited.
- [ ] Validate length, type, URL/domain and identifier bounds before network or storage access.

## Architecture and reliability

- [ ] Put orchestration in a service with injected clock/repository/fetch/delivery ports.
- [ ] Keep HLTV parsing pure and rendering independent from NoneBot events/global storage.
- [ ] Give requests a queue priority, timeout, retry policy and cancellation/shutdown behavior.
- [ ] Make delivery idempotent per destination; partial failure is retryable and observable.
- [ ] Add a versioned migration, backup and rollback/compatibility plan for durable state changes.

## Rendering and accessibility

- [ ] Reuse shared design tokens/components and test long names, missing logos and maximum rows.
- [ ] Verify Chinese/Latin fallback fonts, contrast, image dimensions and output byte-size limits.
- [ ] Keep text fallback for renderer/browser failures when the command can still be useful.

## Evidence and operations

- [ ] Add sanitized fixtures for normal, missing-field and challenge/structure-change pages.
- [ ] Add service tests for success, timeout, partial send failure, retry and idempotency.
- [ ] Add renderer structure tests; use PNG golden tests only for a few stable critical cards.
- [ ] Emit latency, queue depth, fetch outcome, render failure and delivery/retry metrics.
- [ ] Run `python -m pytest`, `ruff check .`, `ruff format --check .` and `pyright`.
- [ ] Write rollout steps, feature flag/default, monitoring window and rollback command.
- [ ] Restart the launchd-managed bot and verify plugin load + OneBot connection after merge.

