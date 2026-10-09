// Canonical-link selection for BFMR reservations. Pure (no prisma): given the
// set of OrderBfmrLink rows recalcBfmrSalePrice is about to sum, drop the
// phantom links that inflate salePrice/bgExpectedPayout -- the no-tracking
// PARENT link left behind after a reservation is split into per-shipment tracked
// children, and any duplicate that re-uses a trackingNumber already owned by
// another link. The dispatched task implements selectCanonicalBfmrLinks in THIS
// file only; recalc wiring is done separately by the reviewer.

export interface BfmrLinkLike {
  id: number;
  reservationId: number;
  trackingNumber: string | null;
}

const isTracked = (l: BfmrLinkLike) => l.trackingNumber !== null && l.trackingNumber !== '';

export function selectCanonicalBfmrLinks<T extends BfmrLinkLike>(links: T[]): T[] {
  // 1. Per reservationId: if the reservation has at least one tracked link,
  //    drop that reservation's no-tracking "parent" link(s) -- superseded by
  //    the split. A reservation with NO tracked link keeps its no-tracking
  //    link (an un-split reservation must NOT be emptied).
  const reservationsWithTracked = new Set<number>();
  for (const l of links) {
    if (isTracked(l)) reservationsWithTracked.add(l.reservationId);
  }
  const parentDropped = links.filter(
    (l) => !reservationsWithTracked.has(l.reservationId) || isTracked(l),
  );

  // 2. A non-null trackingNumber appears at most once in the output; on a
  //    collision keep the link with the smallest id.
  const keeperByTracking = new Map<string, T>();
  for (const l of parentDropped) {
    if (!isTracked(l)) continue;
    const current = keeperByTracking.get(l.trackingNumber as string);
    if (!current || l.id < current.id) keeperByTracking.set(l.trackingNumber as string, l);
  }

  return parentDropped.filter(
    (l) => !isTracked(l) || keeperByTracking.get(l.trackingNumber as string) === l,
  );
}
