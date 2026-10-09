// payoutMismatch: orders-list badge logic (see lib/payoutMismatch.ts).

import test from 'node:test';
import assert from 'node:assert/strict';

import { payoutMismatch } from './payoutMismatch.ts';
import type { OrderForPaymentStatus } from './paymentStatus.ts';

// Helper to build an OrderForPaymentStatus partial.
function order(overrides: Partial<OrderForPaymentStatus> = {}): OrderForPaymentStatus {
  return {
    lost: false,
    cancelled: false,
    salePriceSynced: false,
    salePrice: null,
    bgPaidAmount: null,
    bgExpectedPayout: null,
    bgCredited: false,
    bfmrStatus: null,
    overdueAt: null,
    buyer: null,
    returns: [],
    commitmentLinks: [],
    bfmrLinks: [],
    ...overrides,
  };
}

test('order-899 partial payment with salePrice==expected is NOT a mismatch', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 899.98, bgExpectedPayout: 899.98, bgPaidAmount: 449.99,
  })), false);
});

test('real short-pay (salePrice 449.99, expected 899.98, paid 449.99) IS flagged', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 449.99, bgExpectedPayout: 899.98, bgPaidAmount: 449.99,
  })), true);
});

test('salePrice 700 vs expected 899.98 flagged', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 700, bgExpectedPayout: 899.98, bgPaidAmount: null,
  })), true);
});

test('sub-cent epsilon (899.98 vs 899.9800000001) not flagged', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 899.98, bgExpectedPayout: 899.9800000001, bgPaidAmount: null,
  })), false);
});

test('no expected -> salePrice vs paid compared', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 400, bgExpectedPayout: null, bgPaidAmount: 500,
  })), true);
});

test('zero paid/expected treated as unset', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 899.98, bgExpectedPayout: 0, bgPaidAmount: 0,
  })), false);
});

test('fully-returned orders never flagged', async () => {
  // Set up returns with quantity >= line quantities (1 bfmrLink unit)
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 100, bgExpectedPayout: 899.98, bgPaidAmount: 449.99,
    returns: [{ status: 'refunded', quantity: 1 }],
    bfmrLinks: [{ quantity: 1 }],
  })), false);
});

test('unprocessed orders never flagged', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'shipped', salePrice: 100, bgExpectedPayout: 899.98, bgPaidAmount: null,
  })), false);
});

test('null salePrice never flagged', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: null, bgExpectedPayout: 899.98, bgPaidAmount: null,
  })), false);
});

test('bgCredited counts as processed', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'received', bgCredited: true, salePrice: 100, bgExpectedPayout: 899.98, bgPaidAmount: null,
  })), true);
});

test('salePriceSynced counts as processed', async () => {
  assert.equal(payoutMismatch(order({
    salePriceSynced: true, salePrice: 100, bgExpectedPayout: 899.98, bgPaidAmount: null,
  })), true);
});

test('ref = expected ?? paid when both set', async () => {
  // salePrice matches expected but not paid -> ref=expected -> no mismatch.
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 899.98, bgExpectedPayout: 899.98, bgPaidAmount: 400,
  })), false);
  // salePrice matches paid but not expected -> ref=expected -> mismatch.
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 400, bgExpectedPayout: 899.98, bgPaidAmount: 400,
  })), true);
});

test('ref = paid when expected is null or <=0', async () => {
  // expected=null -> ref=paid=899.98, salePrice=400 -> mismatch.
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 400, bgExpectedPayout: null, bgPaidAmount: 899.98,
  })), true);
  // expected<=0 (treated as null) -> ref=paid=899.98, salePrice=400 -> mismatch.
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 400, bgExpectedPayout: -1, bgPaidAmount: 899.98,
  })), true);
});

test('no expected and no paid -> ref null -> false', async () => {
  assert.equal(payoutMismatch(order({
    bfmrStatus: 'processed', salePrice: 899.98, bgExpectedPayout: null, bgPaidAmount: null,
  })), false);
});

test('paid MORE than expected with salePrice = paid is not a mismatch (orders 761, 649)', () => {
  assert.equal(payoutMismatch(order({ salePriceSynced: true, salePrice: 900, bgPaidAmount: 900, bgExpectedPayout: 897 })), false);
  assert.equal(payoutMismatch(order({ bfmrStatus: 'paid', salePrice: 179, bgPaidAmount: 179, bgExpectedPayout: 174 })), false);
});
test('short pay is still flagged; salePrice not matching the payment is still flagged', () => {
  assert.equal(payoutMismatch(order({ salePriceSynced: true, salePrice: 1197, bgPaidAmount: 399, bgExpectedPayout: 399 })), true);
  assert.equal(payoutMismatch(order({ salePriceSynced: true, salePrice: 897, bgPaidAmount: 3507, bgExpectedPayout: 3507 })), true);
});
