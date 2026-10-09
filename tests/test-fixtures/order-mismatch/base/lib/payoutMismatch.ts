// Pure payout-mismatch check for the orders list "≠ $X" badge, split out of
// app/orders/page.tsx so it is unit tested with the repo's node --test runner.
//
// The sale price is compared against the reference payout: the expected
// payout when known, else what was actually paid. Comparing expected vs paid
// directly (the old behaviour) false-flagged every partial payment even when
// salePrice matched the expected payout.

import { fullyReturned, PROCESSED_STATUSES, type OrderForPaymentStatus } from './paymentStatus.ts';

export function payoutMismatch(o: OrderForPaymentStatus): boolean {
  if (o.salePrice == null) return false;
  // A fully-returned order resolves outside the group payout flow: salePrice
  // has been recomputed down to the remaining (zero) units while
  // bgExpectedPayout still holds the original figure.
  if (fullyReturned(o)) return false;
  const isProcessed =
    (o.bfmrStatus && PROCESSED_STATUSES.has(o.bfmrStatus.toLowerCase())) || o.bgCredited || o.salePriceSynced;
  if (!isProcessed) return false;
  // Treat 0 as unset (CardCenter orders sometimes carry bgPaidAmount = 0).
  const paid = o.bgPaidAmount != null && o.bgPaidAmount > 0 ? o.bgPaidAmount : null;
  const expected = o.bgExpectedPayout != null && o.bgExpectedPayout > 0 ? o.bgExpectedPayout : null;
  const ref = expected ?? paid;
  if (ref == null) return false;
  // Paid at least what was expected and salePrice records that payment: the
  // group paid in full (a bonus or price bump, not a short pay). Orders 761
  // ($900 paid on an $897 commitment), 925, 649 (+$5 cash bonus), 154 (stale
  // per-package expectation) were flagged for being paid MORE.
  if (paid != null && paid >= ref - 0.01 && Math.abs(o.salePrice - paid) <= 0.01) return false;
  return Math.abs(o.salePrice - ref) > 0.01;
}
