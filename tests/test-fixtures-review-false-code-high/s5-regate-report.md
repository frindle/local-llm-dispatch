# Review: lib/rivian.ts

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `lib/rivian.ts` | The removed `fetchRivianServiceThreads()` call is replaced by an inline `gql` call that does not pas |

## Defects

### [HIGH] The removed `fetchRivianServiceThreads()` call is replaced by an inline `gql` call that does not pass the required `VS_GATEWAY` argument, causing the threads query to hit the wrong endpoint or fail.
`lib/rivian.ts`

```
const threadsData = await gql<{ commsListDiscussions?: RawServiceThread[] | null }>(
      GET_ASYNC_MESSAGE_THREAD_LIST,
      {},
      authHeaders(tokens),
    );
```
**How it fails:** When `fetchRivianServiceState` is called, the `gql` call for `GET_ASYNC_MESSAGE_THREAD_LIST` omits the `VS_GATEWAY` parameter (unlike the subsequent `GET_ACTIVE_REQUESTS` and `QUERY_BY_WORK_ORDER_ID` calls which explicitly pass it). If the GraphQL client requires the gateway parameter to route to the correct service for thread data, this call will fail or return stale/wrong data, causing `threads` to be empty or incorrect, leading to `inService` being false even when a service is in progress.
**Verified trigger:** Call fetchRivianServiceState() with a valid vehicleId when RIVIAN_SERVICE_MODE='1'. The function executes the first gql call: gql<{commsListDiscussions?: RawServiceThread[] | null}>(GET_ASYNC_MESSAGE_THREAD_LIST, {}, authHeaders(tokens)). This call does NOT pass VS_GATEWAY. The subsequent calls (GET_ACTIVE_REQUESTS, QUERY_BY_WORK_ORDER_ID) DO pass VS_GATEWAY. If the GraphQL client implementation requires VS_GATEWAY to route to the correct backend service for thread data, this call will fail (network error, 404, or routing error) or return empty data, resulting in an empty threads array. Consequently, pickActiveWorkOrder(threads) returns null, and the function returns a snapshot with inService=false, even if a service is actually in progress.

## Rejected by verification

Raised during review, then knocked down when checked against the code by a pass that did not write them. Listed for audit, not for action.

- ~~The deletion removes the conditional logic that prevents fetching line items when there is no active work order. The new code only fetches line items if `vid` (vehicleId) is truthy, but does not check if there is an active work order (`active` is null).~~ — The finding claims that the new code fetches line items when there is no active work order, but the diff clearly shows the line-item fetch is inside an `if (active)` block. The old code also had this check (`inService` w

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 4 calls, 55s of model time
- Wall clock 55s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario, 1 rejected by verification

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*