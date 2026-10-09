# TASK: smoke-order-mismatch

## Confirmed defect (observed, not suspected)

The orders list shows an orange '≠ $899.98' badge directly under the $899.98
sale price for order 899, even though salePrice equals bgExpectedPayout (both
899.98) and a partial payment of 449.99 was made. Verified by inspecting the
badge-rendering logic in app/orders/page.tsx: the local payoutMismatch()
compares expected vs paid (expected - paid >= 5) and ignores salePrice, so
any partial payment reads as a mismatch.

## Entry point

lib/payoutMismatch.ts:1

## Required change

CONFIRMED SYMPTOM (the owner, order 899): the orders list shows an orange '≠ $899.98' badge directly under the $899.98 sale price even though salePrice equals bgExpectedPayout (both 899.98) and a partial payment of 449.99 was made. Root cause: the local payoutMismatch(o) in app/orders/page.tsx, when both bgExpectedPayout and bgPaidAmount are set, compares expected vs paid (expected - paid >= 5) and ignores salePrice, so partial payments read as a mismatch. MUST HOLD: create lib/payoutMismatch.ts exporting pure payoutMismatch(o: OrderForPaymentStatus): boolean (type + fullyReturned + PROCESSED_STATUSES imported from './paymentStatus.ts' with the explicit .ts extension) with these rules in order: salePrice null -> false; fullyReturned(o) -> false; not processed (bfmrStatus lowercased in PROCESSED_STATUSES, or bgCredited, or salePriceSynced) -> false; treat bgExpectedPayout<=0 and bgPaidAmount<=0 as unset (null); ref = expected ?? paid; ref null -> false; else return Math.abs(o.salePrice - ref) > 0.01 (cent tolerance, same convention as lib/paymentStatus.ts). Wire it: app/orders/page.tsx deletes its local payoutMismatch function, imports { payoutMismatch } from '@/lib/payoutMismatch' (and drops now-unused fullyReturned/PROCESSED_STATUSES imports), and the badge ref becomes (o.bgExpectedPayout != null && o.bgExpectedPayout > 0) ? o.bgExpectedPayout : o.bgPaidAmount!. Add lib/payoutMismatch.test.ts (node:test, style of lib/paymentStatus.test.ts) covering: order-899 partial payment with salePrice==expected is NOT a mismatch; a real short-pay (salePrice 449.99, expected 899.98, paid 449.99) IS flagged; salePrice 700 vs expected 899.98 flagged; sub-cent epsilon (899.98 vs 899.9800000001) not flagged; no expected -> salePrice vs paid compared; zero paid/expected treated as unset; fully-returned, unprocessed and null-salePrice orders never flagged. Add lib/payoutMismatch.test.ts to the npm test script list in package.json (after lib/paymentStatus.test.ts).

Behaviour that must NOT change:
- `paymentStatus` returns 'none' for cancelled orders (even with buyer set)
- `paymentStatus` returns 'lost' for lost orders
- `paymentStatus` returns 'paid' for synced orders
- `paymentStatus` returns 'partial' when bgPaidAmount < expected - 0.01
- `paymentStatus` returns 'paid' when fullyReturned
- `paymentStatus` returns 'pending' for bgCredited or PROCESSED_STATUSES
- `paymentStatus` returns 'overdue' when isOverdue
- `fullyReturned` checks returns against line quantities
- `PROCESSED_STATUSES` contains: received, pkg_received, pkg received, processed, paid, payment_sent, complete, completed

## Must contain

- `payoutMismatch`
- `export function payoutMismatch(`
- `> 0.01`
- in app/orders/page.tsx: `from '@/lib/payoutMismatch'`
- in package.json: `lib/payoutMismatch.test.ts`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

## Scope

Only edit `lib/payoutMismatch.ts`, `app/orders/page.tsx`, `package.json`, `lib/payoutMismatch.test.ts`; do not edit `verify.sh`, `verify.test.ts` or `TASK.md`.
verify.test.ts is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as
  a header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to an implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
