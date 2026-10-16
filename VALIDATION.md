# Validation — 0.4.0

- Python 3.12: all 36 unit/integration tests pass.
- Built and installed the Python wheel locally with no runtime dependencies.
- Installed-package CLI accepts a 2.5-hour override.
- Offline demo generated HTML, Markdown and JSON with a 0.5-hour override.
- Direct Digikala live fetching succeeded after standard cookie-session handling; see LIVE-VALIDATION.md for the bounded live market run.
- Docker engine unavailable in this environment; Docker configuration has not been executed.
- Telegram delivery not exercised with a real account; failure/retry and channel digest formatting, restart deduplication, rate limiting and unconfirmed delivery are tested using mocks.
- GitHub repository publication and production hosting are not performed by this archive.

The archive contains source, documentation, tests, and a labelled fabricated report and single Telegram post preview. It is not a running hosted service. Production recommendations require fresh independent merchant evidence; default discovery is automatic, but successful equivalence depends on the data published by each shop.

- GitHub Actions runner configuration tested for credentials, preview mode, HTTPS feeds and candidate-provider override. Scheduled-slot deduplication tested against delayed runs.
- Scheduled workflow YAML parsed locally; no real GitHub Actions run or Telegram publication was performed. Cache persistence is best effort, not durable storage.

- Cookie redirect integration tested with a local HTTP server. Live-taxonomy exclusion, membership-only prices, exact merchant variants/warranties, currency conversion and explicit Torob shop-link handling tested.
- Two independent merchant offers successfully exercise the verification calculation in mocked integration tests; this is not evidence of a live discounted product.

## 0.4.1 correction

- 40 tests pass, including brand/model matching with different titles, wrong model/brand/edition/pack rejection, scoped WooCommerce attribute tables and footer exclusion.
- Candidate selection distributes market searches across categories and prioritizes concrete model codes. Defaults expand to six Digikala pages and forty market candidates under the existing time budget.
- Bounded live preview fetched 40 candidates and searched 4; no live deal was verified. Failures included a merchant HTTP 503, unavailable exact market results and incomplete product identity. This does not certify twenty real recommendations.

## 0.4.2 seller-filter fix

- Live Torob response for product 7cab51ae-7833-4c2c-9fa2-2216a48ad603 contained in-stock seller rows with optional installment.providers metadata. The former predicate excluded these sellers incorrectly.
- Removed the optional-payment exclusion; concrete direct merchant Offer, stock, model, variant and warranty validation remain mandatory.
- Duplicate shop IDs no longer consume the independent-shop visit allowance.
- Regression test verifies that two cash offers remain comparable when both shops also offer financing and the first shop appears twice. All 40 tests pass.


## 0.7.0 — 2026-10-16

- npm ci --ignore-scripts and TypeScript build: passed for the pinned Torob MCP.
- Real stdio initialization: 10 tools; malformed product input rejected without an upstream request.
- Live search, details and seller tool: succeeded after an initial timeout. Returned 3 search results and 50 seller offers, loaded_list_complete=true, truncated=true. This checks data access, not equal variants or market-wide minima.
- Unit coverage includes stock unknown/false, ads, unreliable/boolean/zero prices, used items, mismatched warranty/color/model, stale/currency data, unique shops, true saving ranking, captions and source blocks.
- Dockerfile updated but an image build is not verified in this environment (Docker CLI unavailable).

- Python tests: 76 passed, including live local stdio protocol validation against the built MCP (no network in unit tests).
- Live Digikala Anker R50i A3949: market access succeeded, but comparable variant/condition evidence remained incomplete. It was marked partial and no comparable saving was invented.

## 0.8.0

81 unit tests passed. Added quota/variety/overlap tests, a 26-request summary-plus-25-photo test with four-line captions, per-channel price-memory checks at the 5% boundary, and no confirmed-history write after an item timeout.
