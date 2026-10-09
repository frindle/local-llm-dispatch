// Pure order-payment-status logic, split out of app/orders/page.tsx so it
// can be unit tested with the repo's node --test runner (page.tsx is a
// 'use client' component and isn't part of that suite).
//
// Real (non-type-only) imports below use explicit .ts extensions —
// lib/paymentStatus.test.ts runs on Node's built-in test runner with type
// stripping, which needs extension-bearing ESM specifiers to resolve these
// at runtime (see the allowImportingTsExtensions note in tsconfig.json).

import { isFullyReturned } from './returnStatus.ts';
import { isOverdue } from './overdue.ts';

export const PROCESSED_STATUSES = new Set([
  'received', 'pkg_received', 'pkg received', 'processed', 'paid', 'payment_sent', 'complete', 'completed',
]);

/** Subset of the orders-page Order type that paymentStatus/fullyReturned need. */
export type OrderForPaymentStatus = {
  lost: boolean;
  cancelled: boolean;
  salePriceSynced: boolean;
  salePrice: number | null;
  bgPaidAmount: number | null;
  bgExpectedPayout: number | null;
  bgCredited: boolean;
  bfmrStatus: string | null;
  overdueAt: string | null;
  buyer: { name: string } | null;
  returns: { status: string; quantity: number }[];
  commitmentLinks: { quantity: number }[];
  bfmrLinks: { quantity: number }[];
};

function lineQuantities(o: OrderForPaymentStatus) {
  return [...o.bfmrLinks.map(l => l.quantity), ...o.commitmentLinks.map(l => l.quantity)];
}

export function fullyReturned(o: OrderForPaymentStatus) {
  return isFullyReturned(o.returns, lineQuantities(o));
}

export function paymentStatus(
  o: OrderForPaymentStatus,
): 'lost' | 'paid' | 'partial' | 'overdue' | 'pending' | 'none' {
  // Cancelled orders get their own "Cancelled" badge (StatusBadges checks
  // o.cancelled before ever calling this) and are excluded outright from
  // P&L elsewhere (see 52e729d) — so they must not fall through to
  // 'pending' just because o.buyer is still set. Same early-return
  // precedence as the o.lost check below.
  if (o.cancelled) return 'none';
  if (o.lost) return 'lost';
  if (o.salePriceSynced) return 'paid';
  if (o.bgPaidAmount != null && o.bgPaidAmount > 0) {
    const expected = o.bgExpectedPayout ?? o.salePrice;
    if (expected == null || o.bgPaidAmount < expected - 0.01) return 'partial';
    return 'paid';
  }
  if (fullyReturned(o)) return 'paid';
  if (o.bgCredited || (o.bfmrStatus && PROCESSED_STATUSES.has(o.bfmrStatus.toLowerCase()))) return 'pending';
  if (o.overdueAt && isOverdue(o.overdueAt)) return 'overdue';
  if (o.buyer) return 'pending';
  return 'none';
}
