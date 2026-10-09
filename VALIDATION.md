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
