// NEGATIVE CONTROL for test-source-text-harness.py: the shape a re-authored
// rt-bfmr-link-sync-feedback fixture should take. It IMPORTS an exported pure
// function the refimpl extracts from the component and asserts its OUTPUT per
// spec branch -- no case reads the target's source text. (Not run here; the
// detector only scans it.)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { syncResultMessage, syncRequestInit } from './components/BfmrReservationLinker.tsx';

test('(c) manual sync POSTs {trigger:"manual"} as JSON', () => {
  const init = syncRequestInit();
  assert.equal(init.method, 'POST');
  assert.deepEqual(JSON.parse(String(init.body)), { trigger: 'manual' });
  assert.equal((init.headers as Record<string, string>)['Content-Type'], 'application/json');
});

test('(a) webError -> error, not success', () => {
  const r = syncResultMessage({ synced: 770, webError: 'login failed' });
  assert.equal(r.error, 'BFMR tracker-id backfill failed: login failed');
  assert.equal(r.message, undefined);
});

test('(b) partial backfill appends the warning', () => {
  const r = syncResultMessage({ synced: 5, webNeeded: 4, webBackfilled: 1, webRows: 9,
                                webUnmatched: 2, webAmbiguous: 1 });
  assert.equal(r.message,
    'Synced 5 -- 3 reservation(s) still lack a BFMR tracker id (unmatched 2, ambiguous 1)');
});

test('edge: webNeeded == 0 is a plain success', () => {
  assert.equal(syncResultMessage({ synced: 7, webNeeded: 0, webBackfilled: 0, webRows: 3 }).message,
               'Synced 7');
});

test('edge: webRows == 0 with no webError is a failed backfill', () => {
  assert.equal(syncResultMessage({ synced: 7, webNeeded: 2, webBackfilled: 0, webRows: 0 }).error,
               'BFMR returned 0 tracker rows');
});

test('the source must not import prisma (absence constraint is allowed)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync('./components/BfmrReservationLinker.tsx', 'utf8');
  assert.ok(!src.includes("from '@prisma"), 'component must not import prisma');
});
