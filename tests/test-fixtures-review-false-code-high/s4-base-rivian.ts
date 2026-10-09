import { readFileSync, writeFileSync, existsSync } from 'fs';
import { join } from 'path';
import {
  markRivianReauthRequired, markRivianReauthDueSoon, clearRivianReauthFlags,
  shouldPushApiErrorOnce, clearRivianApiErrorDedup,
} from './sessionFlags';
import { loggedFetch } from './apiLog';
import { sendPush } from './pushover';
import { VEHICLE_STATE_FIELDS } from '../server/rivian-vehicle-state-fields.js';

const GATEWAY = 'https://rivian.com/api/gql/gateway/graphql';
// vehicleState (via GATEWAY above) has no power/current field at all --
// confirmed 2026-08-08. The classic chrg/user/graphql GraphQL service
// (getLiveSessionData/getLiveSessionHistory/getNonRivianUserSession) turned
// out to be a dead end for home AC charging (all stale/removed or returned
// empty data, see Claude/API-Docs/Rivian.md in the vault for the full
// investigation). Live power actually comes from Rivian's Parallax
// websocket service instead -- see server/parallax-monitor.js and
// readRivianParallaxState() below.

// Rivian sessions appear to last on the order of 6 months (180 days) with no
// documented refresh mutation. Track from savedAt so we can warn the user at
// day 173 and hard-flag at day 180 before we start seeing 401s in the wild.
const RIVIAN_SESSION_DAYS = 180;
const RIVIAN_SESSION_WARN_DAYS = 7;

export function checkRivianSessionAge(): { daysOld: number; daysLeft: number } | null {
  const t = readRivianTokens();
  if (!t) return null;
  const daysOld = Math.floor((Date.now() - t.savedAt) / (24 * 60 * 60 * 1000));
  const daysLeft = RIVIAN_SESSION_DAYS - daysOld;
  if (daysLeft <= 0) {
    markRivianReauthRequired(`session age ${daysOld}d ≥ ${RIVIAN_SESSION_DAYS}d`);
  } else if (daysLeft <= RIVIAN_SESSION_WARN_DAYS) {
    markRivianReauthDueSoon(daysLeft);
  }
  return { daysOld, daysLeft };
}

// Exported so a successful login callback can wipe the reauth flags.
export function noteRivianAuthRefreshed(): void {
  clearRivianReauthFlags();
}

// ── Exponential backoff on state-poll errors ─────────────────────────────
// Only real rate-limiting (GraphQL extensions.code === "RATE_LIMIT", per
// github.com/bretterer/rivian-python-client) gets this long ladder. A
// generic transient failure (e.g. a one-off INTERNAL_SERVER_ERROR) is NOT
// throttling and must not cost up to 15-30+ min of blindness -- confirmed
// 2026-08-07 this exact gap silently ate a real garage-lights-on-arrival
// trigger (car was 7km out, hit one 500, then sat on stale cache the rest
// of the drive since backoff blocked every re-poll). Non-throttle errors
// now just fall through to the normal poll-interval retry (see
// RIVIAN_INTERVAL_*_MS in app/api/dashboard/route.ts) instead.
const BACKOFF_STEPS_MIN = [15, 30, 60, 120, 240];
let backoffAttempt = 0;
let nextAllowedAt = 0;

// Consecutive fetch failures regardless of type (throttle or not) -- purely
// for the "alert if this keeps happening" signal below, separate from the
// throttle-only backoff clock above.
const API_ERROR_ALERT_THRESHOLD = 3;
let consecutiveApiErrors = 0;

// Persistent failure threshold - after this many non-throttle failures in a row,
// we assume opaque throttling or dead endpoint and escalate to backoff instead
// of hammering the endpoint. This prevents persistent "fetch failed" errors from
// causing endless retries.
const PERSISTENT_FAILURE_BACKOFF_THRESHOLD = 3;

function nextBackoffMs(): number {
  const idx = Math.min(backoffAttempt, BACKOFF_STEPS_MIN.length - 1);
  return BACKOFF_STEPS_MIN[idx] * 60 * 1000;
}

function inBackoffWindow(): boolean {
  return Date.now() < nextAllowedAt;
}

function recordBackoffError(): void {
  // nextBackoffMs() reads backoffAttempt BEFORE it's incremented, so the
  // first-ever failure gets BACKOFF_STEPS_MIN[0] (15m) as documented above,
  // not [1] (30m) -- incrementing first was skipping the 15m step entirely.
  const appliedMs = nextBackoffMs();
  nextAllowedAt = Date.now() + appliedMs;
  backoffAttempt = Math.min(backoffAttempt + 1, BACKOFF_STEPS_MIN.length);
  console.warn(`[rivian] backoff step ${backoffAttempt}, next attempt in ${appliedMs / 60000}m`);
}

// Surfaced on the dashboard as a status pill (see rivianApiDegraded in
// app/api/dashboard/route.ts) -- true while a real rate-limit backoff is
// active OR failures have kept happening consecutively past the alert
// threshold, so the display shows *something's* wrong even before it
// crosses the Pushover threshold.
export function rivianApiDegraded(): boolean {
  // A live websocket subscription means the data is current regardless of
  // what the poll path is doing -- poll backoff while pushes are arriving is
  // not a degradation, it's the fallback correctly standing down. Only call
  // it degraded when the push path ISN'T covering for it.
  if (rivianPushFresh()) return false;
  return inBackoffWindow() || consecutiveApiErrors > 0;
}

function resetBackoff(): void {
  if (backoffAttempt !== 0) {
    console.log('[rivian] backoff reset after successful fetch');
  }
  backoffAttempt = 0;
  nextAllowedAt = 0;
  consecutiveApiErrors = 0;
  clearRivianApiErrorDedup();
}

const BASE_HEADERS = {
  'User-Agent': 'RivianApp/707 CFNetwork/1237 Darwin/20.4.0',
  'Accept': 'application/json',
  'Content-Type': 'application/json',
  'Apollographql-Client-Name': 'com.rivian.ios.consumer-apollo-ios',
};

export interface RivianTokens {
  accessToken: string;
  refreshToken: string;
  userSessionToken: string;
  appSessionToken: string;
  csrfToken: string;
  vehicleId: string;
  savedAt: number;
}

export interface RivianVehicleState {
  chargePercent: number;      // batteryLevel.value (0-100)
  chargeLimit: number;        // batteryLimit.value (0-100)
  isCharging: boolean;
  isPluggedIn: boolean;
  isThrottled: boolean;       // chargerDerateStatus indicates active throttling
  derateReason: string;       // raw chargerDerateStatus value
  chargingState: string;      // chargerState.value raw string
  isLocked: boolean;           // true only when all 4 doors report locked
  doorFrontLeftOpen: boolean;
  doorFrontLeftLocked: boolean;
  doorFrontRightOpen: boolean;
  doorFrontRightLocked: boolean;
  doorRearLeftOpen: boolean;
  doorRearLeftLocked: boolean;
  doorRearRightOpen: boolean;
  doorRearRightLocked: boolean;
  anyDoorOpen: boolean;
  anyDoorUnlocked: boolean;
  twelveVoltBatteryHealth: string; // raw value, semantics unconfirmed — log-only until we see a real reading
  climateOn: boolean;
  rangeMi: number;            // distanceToEmpty.value (kilometers) → miles
  odometer: number;           // vehicleMileage.value (miles)
  chargeRateMph: number;      // not reported by Rivian API — always 0
  addedRangeMi: number;       // not reported by Rivian API — always 0
  minutesToFull: number;      // timeToEndOfCharge.value
  online: boolean;
  lat: number | null;         // gnssLocation.latitude
  lon: number | null;         // gnssLocation.longitude
  gnssTimeStamp: string | null;
  gnssSpeedMph: number | null;
  gnssAltitudeM: number | null;
  gnssErrorM: number | null;
  gnssBearingDeg: number | null; // heading -- gnssError.bearing was already queried but never extracted
  powerState: string;            // raw powerState.value, e.g. "go" | "ready" -- already logged, not exposed until now
  hvThermalEvent: string;     // batteryHvThermalEvent raw
  hvThermalPropagation: string; // batteryHvThermalEventPropagation raw
  hvThermalActive: boolean;   // true only for a genuine excursion, not the idle "off"/"nominal" values
  wiperFluidState: string;    // '' | 'normal' | 'low'
  brakeFluidLow: boolean;
  tirePressureFL: string;     // 'normal' | 'low' | 'critical' | ''
  tirePressureFR: string;
  tirePressureRL: string;
  tirePressureRR: string;
  otaCurrentVersion: string;  // otaCurrentVersionNumber
  otaAvailableVersion: string;// otaAvailableVersionNumber
  otaStatus: string;          // otaStatus / otaCurrentStatus
  otaUpdateAvailable: boolean;
  otaInstalling: boolean;
  gearStatus: string;          // raw gearStatus.value — 'park' | 'drive' | ... (confirmed via logs 2026-07-19)
  totalChargedEnergyKwh: number | null;
  displayStatus: number | null;
  evseType: number | null;
}

// ── Token storage ─────────────────────────────────────────────────────────────

function tokensPath(): string {
  const dir = process.env.KEYS_DIR ?? join(process.cwd(), 'keys');
  return join(dir, 'rivian-tokens.json');
}

function loginDebugPath(): string {
  const dir = process.env.KEYS_DIR ?? join(process.cwd(), 'keys');
  return join(dir, 'rivian-login-debug.json');
}

// ── Parallax charging power (separate service, see server/parallax-monitor.js) ──
// vehicleState has no power/current field at all -- confirmed 2026-08-08.
// Rivian's own app sources live charging power from this genuinely
// separate websocket service instead; the monitor process persists the
// latest decoded values here so this read stays a plain file read, no
// websocket client on the request path.
const PARALLAX_STALE_MS = 5 * 60_000; // monitor pushes every ~15-60s while charging
const PARALLAX_POWER_STALE_MS = 90_000; // powerKw only valid if a power msg arrived this recently

export interface RivianParallaxState {
  powerKw: number | null;
  totalChargedEnergyKwh: number | null;
  timeToEndOfChargeSec: number | null;
  chargingStateEnum: number | null;
  plugConnectionStatus: number | null;
  displayStatus: number | null;
  evseType: number | null;
  fresh: boolean; // false = no update within PARALLAX_STALE_MS, don't trust powerKw
  powerKwAt: number | null;
}

export function readRivianParallaxState(): RivianParallaxState | null {
  const dir = process.env.KEYS_DIR ?? join(process.cwd(), 'keys');
  const path = join(dir, 'rivian-parallax.json');
  if (!existsSync(path)) return null;
  try {
    const raw = JSON.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>;
    const updatedAt = typeof raw.updatedAt === 'number' ? raw.updatedAt : 0;
    const powerKwAt = typeof raw.powerKwAt === 'number' ? raw.powerKwAt : null;
    const powerKw = (typeof raw.powerKw === 'number' && 
      powerKwAt !== null && 
      Date.now() - powerKwAt < PARALLAX_POWER_STALE_MS) 
      ? raw.powerKw 
      : null;
    return {
      powerKw,
      totalChargedEnergyKwh: typeof raw.totalChargedEnergyKwh === 'number' ? raw.totalChargedEnergyKwh : null,
      timeToEndOfChargeSec: typeof raw.timeToEndOfChargeSec === 'number' ? raw.timeToEndOfChargeSec : null,
      chargingStateEnum: typeof raw.chargingStateEnum === 'number' ? raw.chargingStateEnum : null,
      plugConnectionStatus: typeof raw.plugConnectionStatus === 'number' ? raw.plugConnectionStatus : null,
      displayStatus: typeof raw.displayStatus === 'number' ? raw.displayStatus : null,
      evseType: typeof raw.evseType === 'number' ? raw.evseType : null,
      fresh: Date.now() - updatedAt < PARALLAX_STALE_MS,
      powerKwAt
    };
  } catch {
    return null;
  }
}

// ── Parallax-first state override ────────────────────────────────────────────
// The classic GraphQL vehicleState lags and is unreliable for home L2 charging,
// while the Parallax websocket stream is live. Enum mapping derived empirically
// from 4899 logged frames (vault Claude/API-Docs/Rivian.md):
//   chargingStateEnum === 3 → actively charging (only state where energy rises)
//   chargingStateEnum === 5 → charge complete / holding at target (still plugged)
//   chargingStateEnum === 1 → idle
//   plugConnectionStatus === 2 → plugged in; === 1 → unplugged
// CRITICAL: powerKw and totalChargedEnergyKwh FREEZE at their last non-zero
// value for hours after a session ends -- never derive isCharging from
// powerKw > 0. The enum is strictly more reliable than power here.
export function applyParallaxToRivianState(
  state: RivianVehicleState | null,
  parallax: RivianParallaxState | null,
): RivianVehicleState | null {
  // Fallback: no state, no parallax data, or stale -- preserve today's
  // classic-API behavior unchanged.
  if (state === null || parallax === null || parallax.fresh !== true) return state;

  // Corroboration veto: the Parallax enum can be FROZEN at 3 with powerKw
  // frozen at its last value for hours after a session ends (the monitor
  // advances updatedAt on heartbeats even when decoded values are unchanged),
  // so fresh === true does not prove the enum reflects the current session.
  // The vehicle's own state is authoritative here: a car reporting powerState
  // 'sleep' cannot be actively charging, and 'charging_scheduled' is an
  // explicit "plugged, scheduled, not yet charging" state.
  const vehicleNotCharging = state.powerState === 'sleep' || state.chargingState === 'charging_scheduled';
  const isCharging = parallax.chargingStateEnum === 3 && !vehicleNotCharging;
  const isPluggedIn = parallax.plugConnectionStatus === 2;
  let chargingState: string;
  if (!isPluggedIn) {
    chargingState = 'disconnected';
  } else if (isCharging) {
    chargingState = 'charging';
  } else if (parallax.chargingStateEnum === 5) {
    chargingState = 'charge_complete';
  } else {
    chargingState = 'not_charging';
  }
  const minutesToFull = (isCharging && typeof parallax.timeToEndOfChargeSec === 'number')
    ? Math.round(parallax.timeToEndOfChargeSec / 60)
    : state.minutesToFull;

  const totalChargedEnergyKwh = isCharging ? (parallax.totalChargedEnergyKwh ?? null) : null;
  const displayStatus = parallax.displayStatus ?? null;
  const evseType = parallax.evseType ?? null;

  return { ...state, isCharging, isPluggedIn, chargingState, minutesToFull, totalChargedEnergyKwh, displayStatus, evseType };
}

// Rivian's mobile API is unofficial/reverse-engineered and known to change
// without notice. Persist the full raw GetCurrentUser response (success or
// failure, every login attempt — overwritten each time) so there's always a
// real reference to diff against instead of relying on scrollback logs.
function writeLoginDebug(entry: Record<string, unknown>): void {
  try {
    writeFileSync(loginDebugPath(), JSON.stringify({ ts: new Date().toISOString(), ...entry }, null, 2));
  } catch { /* non-fatal */ }
}

function vehicleStateDebugPath(): string {
  const dir = process.env.KEYS_DIR ?? join(process.cwd(), 'keys');
  return join(dir, 'rivian-state-debug.json');
}

// Full raw GetVehicleState response, overwritten every successful poll --
// same reasoning as writeLoginDebug above. The trimmed RivianVehicleState
// this module builds only carries fields it currently knows to check; when
// something unexpected happens (an undocumented derate string, a field
// that doesn't parse as assumed), this is the whole return to diff
// against. Persisted to the keys volume so it survives a container
// restart -- unlike stdout logs, which vanish the moment the container
// does (confirmed the hard way 2026-08-08: a redeploy mid-investigation
// erased the only record of the charge session being debugged).
function writeVehicleStateDebug(vs: RawVehicleState): void {
  try {
    writeFileSync(vehicleStateDebugPath(), JSON.stringify({ ts: new Date().toISOString(), vehicleState: vs }, null, 2));
  } catch { /* non-fatal */ }
}

export function readRivianTokens(): RivianTokens | null {
  const p = tokensPath();
  if (!existsSync(p)) return null;
  try {
    return JSON.parse(readFileSync(p, 'utf-8')) as RivianTokens;
  } catch {
    return null;
  }
}

export function writeRivianTokens(tokens: RivianTokens): void {
  writeFileSync(tokensPath(), JSON.stringify(tokens, null, 2));
}

// ── GraphQL helper ────────────────────────────────────────────────────────────

function opName(query: string): string {
  // Extract the GraphQL operation name for logging so /api/admin/api-stats
  // can bucket by call type (Login, GetVehicleState, etc.) rather than
  // seeing every Rivian call as one opaque "graphql".
  const m = query.match(/^\s*(?:query|mutation|subscription)\s+(\w+)/);
  return m?.[1] ?? 'anonymous';
}

async function gql<T>(
  query: string,
  variables: Record<string, unknown> = {},
  extraHeaders: Record<string, string> = {},
  url: string = GATEWAY,
): Promise<T> {
  const res = await loggedFetch('rivian', opName(query), url, {
    method: 'POST',
    headers: { ...BASE_HEADERS, ...extraHeaders },
    body: JSON.stringify({ query, variables }),
    signal: AbortSignal.timeout(15000),
  }, { backoffActive: inBackoffWindow() });
  const json = await res.json() as { data?: T; errors?: unknown[] };
  if (json.errors?.length) throw new Error(JSON.stringify(json.errors[0]));
  if (!json.data) throw new Error('No data in response');
  return json.data;
}

// ── Auth flow ─────────────────────────────────────────────────────────────────

const CREATE_CSRF = `
mutation CreateCSRFToken {
  createCsrfToken { csrfToken appSessionToken }
}`;

const LOGIN = `
mutation Login($email: String!, $password: String!) {
  login(email: $email, password: $password) {
    __typename
    ... on MobileLoginResponse { accessToken refreshToken userSessionToken }
    ... on MobileMFALoginResponse { otpToken }
  }
}`;

const LOGIN_OTP = `
mutation LoginWithOTP($email: String!, $otpToken: String!, $otpCode: String!) {
  loginWithOTP(email: $email, otpToken: $otpToken, otpCode: $otpCode) {
    accessToken refreshToken userSessionToken
  }
}`;

export interface LoginResult {
  type: 'success' | 'otp_required';
  otpToken?: string;
}

export async function rivianLogin(email: string, password: string): Promise<LoginResult & { csrfToken?: string; appSessionToken?: string }> {
  // Step 1: CSRF token
  const csrf = await gql<{ createCsrfToken: { csrfToken: string; appSessionToken: string } }>(CREATE_CSRF);
  const { csrfToken, appSessionToken } = csrf.createCsrfToken;

  const authHeaders = {
    'Csrf-Token': csrfToken,
    'A-Sess': appSessionToken,
  };

  // Step 2: Login
  const loginData = await gql<{
    login: {
      __typename: string;
      accessToken?: string;
      refreshToken?: string;
      userSessionToken?: string;
      otpToken?: string;
    };
  }>(LOGIN, { email, password }, authHeaders);

  const login = loginData.login;

  if (login.__typename === 'MobileMFALoginResponse' && login.otpToken) {
    return { type: 'otp_required', otpToken: login.otpToken, csrfToken, appSessionToken };
  }

  if (login.__typename === 'MobileLoginResponse' && login.accessToken) {
    const tokens = await resolveTokensAndVehicle({
      accessToken: login.accessToken,
      refreshToken: login.refreshToken!,
      userSessionToken: login.userSessionToken!,
      appSessionToken,
      csrfToken,
    });
    writeRivianTokens(tokens);
    return { type: 'success' };
  }

  throw new Error('Unexpected login response');
}

export async function rivianLoginOtp(
  email: string,
  otpToken: string,
  otpCode: string,
  csrfToken: string,
  appSessionToken: string,
): Promise<void> {
  const authHeaders = { 'Csrf-Token': csrfToken, 'A-Sess': appSessionToken };
  const data = await gql<{
    loginWithOTP: { accessToken: string; refreshToken: string; userSessionToken: string };
  }>(LOGIN_OTP, { email, otpToken, otpCode }, authHeaders);

  const { accessToken, refreshToken, userSessionToken } = data.loginWithOTP;
  const tokens = await resolveTokensAndVehicle({
    accessToken,
    refreshToken,
    userSessionToken,
    appSessionToken,
    csrfToken,
  });
  writeRivianTokens(tokens);
}

// ── Vehicle lookup ────────────────────────────────────────────────────────────

const GET_USER_VEHICLES = `
query GetCurrentUser {
  currentUser {
    id
    vehicles {
      id
      name
      vin
      vehicle { id vin make model modelYear }
    }
  }
}`;

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function resolveTokensAndVehicle(partial: Omit<RivianTokens, 'vehicleId' | 'savedAt'>): Promise<RivianTokens> {
  const authHeaders = {
    'Csrf-Token': partial.csrfToken,
    'A-Sess': partial.appSessionToken,
    'U-Sess': partial.userSessionToken,
  };

  let vehicleId = '';
  // Confirmed from a real login (2026-07-18): GetCurrentUser succeeds
  // (200 OK) immediately after auth but returns currentUser.vehicles: []
  // — the query shape matches Rivian's documented schema exactly, so this
  // isn't a wrong-field bug, it's the account's vehicle list not yet
  // propagated to the brand-new session. Retry a few times with a short
  // delay before giving up.
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const userData = await gql<{
        currentUser: { vehicles: Array<{ id: string; name: string; vin: string; vehicle?: { id: string } }> };
      }>(GET_USER_VEHICLES, {}, authHeaders);

      const v0 = userData.currentUser.vehicles[0];
      vehicleId = v0?.id || v0?.vehicle?.id || '';
      writeLoginDebug({ attempt, ok: true, vehicleId, response: userData });
      if (vehicleId) break;

      console.warn(`[rivian] resolveTokensAndVehicle: attempt ${attempt}/4 got no vehicles yet:`, JSON.stringify(userData));
    } catch (e) {
      writeLoginDebug({ attempt, ok: false, error: String(e) });
      console.error(`[rivian] resolveTokensAndVehicle: attempt ${attempt}/4 GetCurrentUser failed:`, e);
    }
    if (attempt < 4) await sleep(3000);
  }
  if (!vehicleId) {
    console.error('[rivian] resolveTokensAndVehicle: giving up after 4 attempts, no vehicle ID resolved');
  }

  return { ...partial, vehicleId, savedAt: Date.now() };
}

// Retry vehicle-ID resolution using the already-saved session tokens — no
// re-login/OTP needed. Recovery path if the propagation delay above (up to
// ~12s) still wasn't enough; the tokens themselves are already valid.
export async function reresolveVehicleId(): Promise<{ ok: boolean; vehicleId: string }> {
  const tokens = readRivianTokens();
  if (!tokens) return { ok: false, vehicleId: '' };
  const updated = await resolveTokensAndVehicle(tokens);
  writeRivianTokens(updated);
  return { ok: updated.vehicleId !== '', vehicleId: updated.vehicleId };
}

// ── Vehicle state ─────────────────────────────────────────────────────────────

// Field selection lives in server/rivian-vehicle-state-fields.js so the
// polled query here and the pushed subscription in
// server/rivian-state-monitor.js can never drift apart -- both feed
// mapRawVehicleState() below, which assumes one field set.
const GET_VEHICLE_STATE = `
query GetVehicleState($vehicleID: String!) {
  vehicleState(id: $vehicleID) {${VEHICLE_STATE_FIELDS}
  }
}`;

interface RawVehicleState {
  cloudConnection: { lastSync: string; isOnline?: boolean };
  batteryLevel: { value: number } | null;
  distanceToEmpty: { value: number } | null;
  batteryLimit: { value: number } | null;
  timeToEndOfCharge: { value: number } | null;
  chargerState: { value: string; timeStamp?: string } | null;
  chargerStatus: { value: string; timeStamp?: string } | null;
  chargerDerateStatus: { value: string; timeStamp?: string } | null;
  powerState: { value: string; timeStamp?: string } | null;
  gearStatus: { value: string; timeStamp?: string } | null;
  vehicleMileage: { value: number } | null;
  doorFrontLeftLocked: { value: string } | null;
  doorFrontLeftClosed: { value: string } | null;
  doorFrontRightLocked: { value: string } | null;
  doorFrontRightClosed: { value: string } | null;
  doorRearLeftLocked: { value: string } | null;
  doorRearLeftClosed: { value: string } | null;
  doorRearRightLocked: { value: string } | null;
  doorRearRightClosed: { value: string } | null;
  twelveVoltBatteryHealth: { value: string; timeStamp?: string } | null;
  cabinPreconditioningStatus: { value: string } | null;
  chargePortState: { value: string; timeStamp?: string } | null;
  gnssLocation: { latitude: number; longitude: number; timeStamp?: string } | null;
  gnssSpeed: { value: number; timeStamp?: string } | null;
  gnssAltitude: { value: number; timeStamp?: string } | null;
  gnssError: { positionHorizontal?: number; positionVertical?: number; speed?: number; bearing?: number; timeStamp?: string } | null;
  wiperFluidState: { value: string; timeStamp?: string } | null;
  brakeFluidLow: { value: boolean | string; timeStamp?: string } | null;
  tirePressureStatusFrontLeft: { value: string; timeStamp?: string } | null;
  tirePressureStatusFrontRight: { value: string; timeStamp?: string } | null;
  tirePressureStatusRearLeft: { value: string; timeStamp?: string } | null;
  tirePressureStatusRearRight: { value: string; timeStamp?: string } | null;
  batteryHvThermalEvent: { value: string; timeStamp?: string } | null;
  batteryHvThermalEventPropagation: { value: string; timeStamp?: string } | null;
  otaCurrentVersionNumber: { value: string; timeStamp?: string } | null;
  otaAvailableVersionNumber: { value: string; timeStamp?: string } | null;
  otaStatus: { value: string; timeStamp?: string } | null;
  otaCurrentStatus: { value: string; timeStamp?: string } | null;
}

function authHeaders(t: RivianTokens): Record<string, string> {
  return {
    'Csrf-Token': t.csrfToken,
    'A-Sess': t.appSessionToken,
    'U-Sess': t.userSessionToken,
  };
}

// ── Raw -> RivianVehicleState mapping ─────────────────────────────────────
// Deliberately pulled out of fetchRivianVehicleState so BOTH ingest paths --
// the REST poll below and the pushed websocket subscription
// (server/rivian-state-monitor.js -> readRivianPushState()) -- go through
// exactly one set of derivations. Every quirk documented in here (Rivian
// sending JSON numbers on String-typed fields, chargerState's timeStamp
// being last-*changed*, the idle-vs-active enum sets) applies identically to
// pushed frames, and duplicating it per path is how they'd silently drift.
//
// Null-tolerant by construction: every read is `vs.field?.value ?? <default>`,
// so a payload where most fields are null or absent -- which is exactly what
// both paths return while the vehicle is asleep or in service -- degrades to
// defaults instead of throwing.
export function mapRawVehicleState(vs: RawVehicleState, source: 'poll' | 'push' = 'poll'): RivianVehicleState {
  const src = source === 'push' ? 'rivian-ws' : 'rivian';
  // String() on every enum-ish field: Rivian's gateway returns raw JSON
  // numbers on fields its own schema types as String (confirmed on the OTA
  // version fields), and any .trim()/.toLowerCase() on one of those throws
  // out of this whole function — which the caller then reads as a poll
  // failure and answers with a stale-cache serve.
  const chargingStateRaw = String(vs.chargerState?.value ?? 'disconnected');
  const chargerStateTs = vs.chargerState?.timeStamp;
  const chargerStatusRaw = String(vs.chargerStatus?.value ?? '');
  const chargerStatusTs = vs.chargerStatus?.timeStamp;
  const chargePortRaw = vs.chargePortState?.value ?? '';
  const chargePortTs = vs.chargePortState?.timeStamp;
  const powerStateRaw = vs.powerState?.value ?? '';
  const powerStateTs = vs.powerState?.timeStamp;
  // Log-only for now — observing real values to confirm the "driving" vs
  // "parked" strings before building the garage-light-on-arrival automation
  // off of it. Not read anywhere else yet.
  const gearStatusRaw = vs.gearStatus?.value ?? '';
  const gearStatusTs = vs.gearStatus?.timeStamp;

  const derateRawEarly = vs.chargerDerateStatus?.value ?? '';
  const hvThermalRaw = String(vs.batteryHvThermalEvent?.value ?? '');
  const hvThermalPropRaw = String(vs.batteryHvThermalEventPropagation?.value ?? '');
  const wiperFluidRaw = vs.wiperFluidState?.value ?? '';
  const brakeFluidRaw = vs.brakeFluidLow?.value;
  const tpFL = vs.tirePressureStatusFrontLeft?.value ?? '';
  const tpFR = vs.tirePressureStatusFrontRight?.value ?? '';
  const tpRL = vs.tirePressureStatusRearLeft?.value ?? '';
  const tpRR = vs.tirePressureStatusRearRight?.value ?? '';
  const gnssErrH = vs.gnssError?.positionHorizontal;
  console.log(
    `[${src}] chargerState="${chargingStateRaw}"@${chargerStateTs ?? '?'} ` +
    `chargerStatus="${chargerStatusRaw}"@${chargerStatusTs ?? '?'} ` +
    `chargePortState="${chargePortRaw}"@${chargePortTs ?? '?'} ` +
    `powerState="${powerStateRaw}"@${powerStateTs ?? '?'} ` +
    `gearStatus="${gearStatusRaw}"@${gearStatusTs ?? '?'} ` +
    `derate="${derateRawEarly}" hvThermal="${hvThermalRaw}" hvProp="${hvThermalPropRaw}" ` +
    `tires=FL:${tpFL}/FR:${tpFR}/RL:${tpRL}/RR:${tpRR} ` +
    `wiper="${wiperFluidRaw}" brakeLow=${brakeFluidRaw} ` +
    `gnssErrH=${gnssErrH ?? '?'} online=${vs.cloudConnection?.isOnline ?? '?'}`
  );

  // Resolve plug status from chargerStatus alone — matches the proven
  // approach in Home Assistant's Rivian integration (bretterer/home-assistant-rivian,
  // coordinator.py): `chargerStatus.value != "chrgr_sts_not_connected"`.
  //
  // chargePortState is NOT a plug signal despite the name — HA's own
  // integration maps it to a separate DOOR-class sensor (open/closed
  // charge port door), unrelated to whether a cable is connected.
  // Earlier logic here treated door-closed as unplugged and overrode a
  // correctly-connected chargerStatus, which is why the dashboard could
  // show "not plugged in" while the car was actually charging.
  const isPluggedIn = chargerStatusRaw !== '' && chargerStatusRaw !== 'chrgr_sts_not_connected';

  // The old rule was `!chargerStateStale && CHARGING_ACTIVE.has(chargerState)`,
  // where "stale" meant chargerState.timeStamp older than 15 min. That
  // timestamp is a last-*changed* stamp, so a steady multi-hour charge ages
  // out of the window and the veto flipped isCharging to false while the
  // car was demonstrably still charging — the reported "IDLE · PLUGGED IN ·
  // NOT CHARGING". isPluggedIn had no such veto, which is exactly why the
  // plug state stayed right while the charging state went wrong.
  //
  // Age was always a proxy for the real question the veto existed to ask:
  // "is this chargerState value still describing reality?" isPluggedIn
  // answers that directly and doesn't decay — an unplugged car can't be
  // charging no matter what a stale chargerState still says. So veto on
  // the plug state instead of on the clock.
  const CHARGING_ACTIVE = new Set(['charging', 'charging_active', 'charge_starting', 'charge_active', 'charging_ac_1ph', 'charging_ac_3ph']);
  // chargerStatus, if it reports charging at all, is the more direct signal
  // (it's the same field isPluggedIn trusts). Only `chrgr_sts_not_connected`
  // is confirmed from primary sources — the "connected and charging" literal
  // is NOT, so match it tolerantly and fall through to chargerState when it
  // doesn't hit rather than hard-coding a guessed enum value. Confirm the
  // real string from the `[rivian] ... chargerStatus="…"` log line above
  // during a live charge, then tighten this.
  const statusSaysCharging = /charging/.test(chargerStatusRaw)
    && !/not_charging|no_chrg/.test(chargerStatusRaw);
  const isCharging = statusSaysCharging
    || (isPluggedIn && CHARGING_ACTIVE.has(chargingStateRaw.toLowerCase()));

  // Rivian charger derate (throttling). Treat anything that's not empty
  // / "no_derate" / "none" / "inactive" as throttled. Specific reason
  // strings are surfaced verbatim — we don't have a documented enum.
  // String() because Rivian does send raw JSON numbers on fields its own
  // schema types as String (already confirmed for the OTA version fields
  // below). A numeric value here made .trim() throw, and the outer catch
  // turned that into a backoff step + stale-cache serve — i.e. the entire
  // Rivian card silently frozen on its last good poll, throttle included.
  const derateRaw = String(vs.chargerDerateStatus?.value ?? '').trim();
  const derateLower = derateRaw.toLowerCase();
  // '0'/'false' are here because of the String() above: a numeric-0 "not
  // derated" must not read as a throttle reason now that it survives to
  // this comparison instead of throwing.
  const DERATE_IDLE = new Set(['', 'no_derate', 'none', 'inactive', 'normal', '0', 'false']);
  const isThrottled = !DERATE_IDLE.has(derateLower);

  // HV thermal event/propagation: same shape as derate above — Rivian
  // returns a non-empty idle string ("off" / "nominal", confirmed from
  // container logs 2026-07-18) even with no active excursion, so "not
  // empty" alone false-positives on every poll. Only flag genuine values.
  const HV_IDLE = new Set(['', 'off', 'none', 'no_event', 'inactive', 'normal', 'nominal', '0', 'false']);
  const hvThermalActive =
    !HV_IDLE.has(hvThermalRaw.toLowerCase()) || !HV_IDLE.has(hvThermalPropRaw.toLowerCase());

  // Only show climate as on for explicitly active states; 'system_idle', 'not_available', etc. → off
  const CLIMATE_ACTIVE = new Set(['cooling', 'heating', 'defrost', 'ventilation', 'preconditioning', 'hvac_conditioning']);
  const climateVal = String(vs.cabinPreconditioningStatus?.value ?? '').toLowerCase();

  // Rivian sends these as raw JSON numbers despite the GraphQL schema
  // typing them as strings — confirmed from a real poll (otaCurrent=1,
  // otaAvailable=0). String(...) normalizes both sides so the comparison
  // below can't false-positive on a `0 !== ''` type mismatch, and "0" is
  // treated as "no update queued" the same as an empty value.
  const otaCurrent = String(vs.otaCurrentVersionNumber?.value ?? '');
  const otaAvailable = String(vs.otaAvailableVersionNumber?.value ?? '');
  const otaStatusRaw = (vs.otaStatus?.value ?? vs.otaCurrentStatus?.value ?? '').toString();
  const otaStatusLower = otaStatusRaw.toLowerCase();
  const otaInstalling = /install|download|apply|updating/.test(otaStatusLower);
  const otaUpdateAvailable = otaAvailable !== '' && otaAvailable !== '0' && otaAvailable !== otaCurrent;
  console.log(
    `[${src}-ota] current="${otaCurrent}" available="${otaAvailable}" ` +
    `status="${otaStatusRaw}" updateAvailable=${otaUpdateAvailable}`
  );

  const brakeLowBool = brakeFluidRaw === true || brakeFluidRaw === 'low' || brakeFluidRaw === 'true';

  // Door open/locked value semantics confirmed against Home Assistant's
  // Rivian integration (bretterer/home-assistant-rivian, const.py):
  // *Closed field value "open" means open; *Locked field value "unlocked" means unlocked.
  const doorFrontLeftOpen = vs.doorFrontLeftClosed?.value === 'open';
  const doorFrontLeftLockedBool = vs.doorFrontLeftLocked?.value === 'locked';
  const doorFrontRightOpen = vs.doorFrontRightClosed?.value === 'open';
  const doorFrontRightLockedBool = vs.doorFrontRightLocked?.value === 'locked';
  const doorRearLeftOpen = vs.doorRearLeftClosed?.value === 'open';
  const doorRearLeftLockedBool = vs.doorRearLeftLocked?.value === 'locked';
  const doorRearRightOpen = vs.doorRearRightClosed?.value === 'open';
  const doorRearRightLockedBool = vs.doorRearRightLocked?.value === 'locked';
  const anyDoorOpen = doorFrontLeftOpen || doorFrontRightOpen || doorRearLeftOpen || doorRearRightOpen;
  const anyDoorUnlocked = !doorFrontLeftLockedBool || !doorFrontRightLockedBool || !doorRearLeftLockedBool || !doorRearRightLockedBool;

  // 12V battery health — HA exposes this as a plain diagnostic sensor with
  // no documented enum. Logging the raw value until we see real readings
  // to know what "unhealthy" looks like, same pattern used for gearStatus.
  const twelveVoltRaw = vs.twelveVoltBatteryHealth?.value ?? '';
  if (twelveVoltRaw !== '') console.log(`[${src}] twelveVoltBatteryHealth="${twelveVoltRaw}"`);

  return {
    chargePercent: vs.batteryLevel?.value ?? 0,
    chargeLimit: vs.batteryLimit?.value ?? 80,
    isCharging,
    isPluggedIn,
    isThrottled,
    derateReason: derateRaw,
    chargingState: chargingStateRaw,
    isLocked: doorFrontLeftLockedBool && doorFrontRightLockedBool && doorRearLeftLockedBool && doorRearRightLockedBool,
    doorFrontLeftOpen,
    doorFrontLeftLocked: doorFrontLeftLockedBool,
    doorFrontRightOpen,
    doorFrontRightLocked: doorFrontRightLockedBool,
    doorRearLeftOpen,
    doorRearLeftLocked: doorRearLeftLockedBool,
    doorRearRightOpen,
    doorRearRightLocked: doorRearRightLockedBool,
    anyDoorOpen,
    anyDoorUnlocked,
    twelveVoltBatteryHealth: twelveVoltRaw,
    climateOn: CLIMATE_ACTIVE.has(climateVal),
    rangeMi: vs.distanceToEmpty?.value ? Math.round(vs.distanceToEmpty.value / 1.609344) : 0,
    // vehicleMileage is returned in meters; convert to miles
    odometer: Math.round((vs.vehicleMileage?.value ?? 0) / 1609.344),
    minutesToFull: vs.timeToEndOfCharge?.value ?? 0,
    chargeRateMph: 0,
    addedRangeMi: 0,
    online: vs.cloudConnection?.isOnline ?? false,
    lat: vs.gnssLocation?.latitude ?? null,
    lon: vs.gnssLocation?.longitude ?? null,
    gnssTimeStamp: vs.gnssLocation?.timeStamp ?? null,
    gnssSpeedMph: vs.gnssSpeed?.value != null ? vs.gnssSpeed.value * 2.23694 : null,
    gnssAltitudeM: vs.gnssAltitude?.value ?? null,
    gnssErrorM: vs.gnssError?.positionHorizontal ?? null,
    gnssBearingDeg: vs.gnssError?.bearing ?? null,
    powerState: powerStateRaw,
    hvThermalEvent: hvThermalRaw,
    hvThermalPropagation: hvThermalPropRaw,
    hvThermalActive,
    wiperFluidState: wiperFluidRaw,
    brakeFluidLow: brakeLowBool,
    tirePressureFL: tpFL,
    tirePressureFR: tpFR,
    tirePressureRL: tpRL,
    tirePressureRR: tpRR,
    otaCurrentVersion: otaCurrent,
    otaAvailableVersion: otaAvailable,
    otaStatus: otaStatusRaw,
    otaUpdateAvailable,
    otaInstalling,
    totalChargedEnergyKwh: null,
    displayStatus: null,
    evseType: null,
    gearStatus: gearStatusRaw,
  };
}

export async function fetchRivianVehicleState(vehicleId?: string): Promise<RivianVehicleState | null> {
  const tokens = readRivianTokens();
  if (!tokens) return null;

  const vid = vehicleId ?? tokens.vehicleId;
  if (!vid) {
    // Was a totally silent dead-end before — every poll returned null with
    // zero trace anywhere (no log line, no backoff, no error), so a bad
    // token save could go unnoticed indefinitely. Loud now on purpose.
    console.error('[rivian] fetchRivianVehicleState: no vehicleId on tokens — reconnect Rivian in /admin');
    return null;
  }

  // Rivian throttling is opaque — respect our own backoff clock.
  if (inBackoffWindow()) {
    return null;
  }

  // Piggyback the 90-day session-age check on the poll cycle. Cheap.
  checkRivianSessionAge();

  try {
    const data = await gql<{ vehicleState: RawVehicleState }>(
      GET_VEHICLE_STATE,
      { vehicleID: vid },
      authHeaders(tokens),
    );

    resetBackoff();
    const vs = data.vehicleState;
    writeVehicleStateDebug(vs);
    return mapRawVehicleState(vs, 'poll');
  } catch (e) {
    const msg = String(e);
    // 401 in the error body → session expired. Set the reauth flag so the
    // dashboard can surface a banner.
    if (/401|unauthori[sz]ed|invalid[_ ]session|expired/i.test(msg)) {
      try { markRivianReauthRequired('401 from vehicleState: ' + msg.slice(0, 200)); } catch {}
    }
    // Real rate-limiting carries extensions.code "RATE_LIMIT" (confirmed via
    // github.com/bretterer/rivian-python-client's ERROR_CODE_CLASS_MAP) --
    // this regex already happens to catch that string. Only THIS gets the
    // long backoff ladder; a generic transient failure (e.g. one-off
    // INTERNAL_SERVER_ERROR, confirmed 2026-08-07 to silently eat a real
    // arrival event by blacking out polling for 30min on a single 500) just
    // falls through to the normal poll-interval retry instead.
    const isThrottle = /429|rate[_ ]?limit|too many requests|throttl/i.test(msg);
    if (isThrottle) {
      recordBackoffError();
      console.error('[rivian] THROTTLED:', msg.slice(0, 240));
    } else {
      // Check for persistent non-throttle failures that should trigger backoff
      const nextConsecutiveApiErrors = consecutiveApiErrors + 1;
      if (nextConsecutiveApiErrors >= PERSISTENT_FAILURE_BACKOFF_THRESHOLD) {
        recordBackoffError();
        console.warn('[rivian] fetchRivianVehicleState failed (persistent non-throttle, escalating to backoff):', msg.slice(0, 240));
      } else {
        console.warn('[rivian] fetchRivianVehicleState failed (non-throttle, retrying next cycle):', msg.slice(0, 240));
      }
    }

    consecutiveApiErrors++;
    if (consecutiveApiErrors >= API_ERROR_ALERT_THRESHOLD && shouldPushApiErrorOnce()) {
      void sendPush(
        'EV Dashboard — Rivian API errors persisting',
        `${consecutiveApiErrors} consecutive Rivian API failures. Latest: ${msg.slice(0, 200)}`,
        1,
      );
    }
    return null;
  }
}

// ── Real-time push path (websocket subscription) ──────────────────────────
// server/rivian-state-monitor.js holds a persistent graphql-ws subscription
// to wss://api.rivian.com/gql-consumer-subscriptions/graphql and writes the
// merged vehicleState here. Reading it is a plain file read -- no websocket
// client on the request path, same convention as readRivianParallaxState()
// above and the Tesla telemetry sidecar.
//
// The push path is PREFERRED but never REQUIRED: the poll above stays fully
// wired as the fallback. Staleness of this file is the only switch between
// them (see getRivianVehicleState).
//
// !! Before anyone deletes fetchRivianVehicleState(): the only live capture
// of this subscription was taken while the vehicle was IN SERVICE, when
// Rivian reports nearly every vehicleState field as null. The transport is
// confirmed (handshake, auth with the dashboard's shared account, a real
// stream of `next` frames); FULL FIELD POPULATION IS NOT. Take an
// out-of-service capture and diff a pushed payload against a polled
// GetVehicleState response field by field first. Until then the poll is the
// thing that proves the numbers, and this is the thing that makes them
// timely.
//
// Generous window on purpose: a parked, quiet car legitimately pushes
// nothing for a while, and falling back to the poll's 5-minute idle tier the
// moment it goes quiet would give up most of the benefit. If nothing has
// arrived in this long, though, the socket is more likely wedged than the
// car is quiet -- and the monitor's own idle watchdog (10 min) should have
// reconnected by then, so silence past this point is a real fault.
const PUSH_STALE_MS = 15 * 60_000;

export interface RivianPushState {
  state: RivianVehicleState;
  updatedAt: number;
  ageMs: number;
}

function pushStatePath(): string {
  const dir = process.env.KEYS_DIR ?? join(process.cwd(), 'keys');
  return join(dir, 'rivian-push-state.json');
}

export function readRivianPushState(): RivianPushState | null {
  const p = pushStatePath();
  if (!existsSync(p)) return null;
  try {
    const raw = JSON.parse(readFileSync(p, 'utf-8')) as {
      updatedAt?: number;
      vehicleState?: RawVehicleState | null;
    };
    const updatedAt = typeof raw.updatedAt === 'number' ? raw.updatedAt : 0;
    if (!updatedAt || !raw.vehicleState) return null;
    return {
      // Same mapping as the poll, including its null-tolerance: the monitor
      // merges pushed patches over the last known values but makes no
      // guarantee that every field has ever been seen, so anything never
      // pushed is simply absent and falls through to the same defaults an
      // all-null polled response would produce.
      state: mapRawVehicleState(raw.vehicleState, 'push'),
      updatedAt,
      ageMs: Date.now() - updatedAt,
    };
  } catch {
    // A torn read (monitor mid-write) or a corrupt file must degrade to the
    // poll, never throw into the request path.
    return null;
  }
}

// True while the websocket subscription is delivering. Exported so the
// dashboard route can skip its poll-interval cache gate -- serving a
// minutes-old cached snapshot makes no sense when a fresher one is sitting
// in a local file for free.
export function rivianPushFresh(): boolean {
  const p = readRivianPushState();
  return p !== null && p.ageMs < PUSH_STALE_MS;
}

// The one entry point callers should use for vehicle state.
//   push fresh  -> pushed state, zero network calls, real-time
//   otherwise   -> the REST poll, backoff ladder and all, exactly as before
// Deliberately not "push OR nothing": the socket can be down for reasons
// that have nothing to do with the vehicle (container restart, Rivian
// dropping the subscription, a wedged connection mid-reconnect), and the
// poll is a fully working path that should cover every one of those.
export async function getRivianVehicleState(
  vehicleId?: string,
): Promise<RivianVehicleState | null> {
  const pushed = readRivianPushState();
  if (pushed && pushed.ageMs < PUSH_STALE_MS) {
    return pushed.state;
  }
  if (pushed) {
    console.warn(`[rivian] push state stale (${Math.round(pushed.ageMs / 60000)}m), falling back to poll`);
  }
  return fetchRivianVehicleState(vehicleId);
}

// Clear backoff + reauth flags after a successful login. Called from
// admin login endpoints.
export function noteRivianLoginSuccess(): void {
  resetBackoff();
  noteRivianAuthRefreshed();
}

export function hasRivianTokens(): boolean {
  return readRivianTokens() !== null;
}

// ── Service / "in service" state — SHIPPED DORMANT ───────────────────────────
//
// WHY THIS IS DORMANT (do not wire it up without re-reading this):
// Rivian's service data is OWNER-SCOPED. Confirmed 2026-08-24 by logging the
// same app into the owner's owner account and into the ev-dashboard's SHARED
// (invited-user) account against the same VIN:
//
//   op                        | owner acct | dashboard (shared) acct
//   CommsListDiscussions      | 16 threads | 0
//   GetAsyncMessageThreadList | 16 threads | 0
//   GetActiveRequests         |  5 items   | 0
//
// ev-dashboard logs in with the SHARED account, so every one of the calls
// below returns EMPTY for it today. Turning this on would just add API traffic
// and a permanently-false "in service" answer, so it is gated off and is NOT
// called from the poll loop (app/api/dashboard/route.ts) or any UI path.
//
// WHAT FLIPS IT LIVE — exactly two things, in order:
//   1. The owner's PENDING TEST resolves positive: the ev-dashboard account is
//      provisioned as a CREDENTIALED driver WITH A PHONE KEY on the R1S, and a
//      re-capture shows these three ops returning non-empty data for it.
//      (Hypothesis basis: GetVehicle.invitedUsers[] carries `isCredentialed`
//      + `roles[]`, so access may be gated on being keyed, not strictly on
//      being the owner.) If it resolves negative, the only path is the OWNER
//      account's credentials — a separate security decision, not a code change.
//   2. Set env RIVIAN_SERVICE_MODE=1 on the container, then have a caller
//      invoke fetchRivianServiceState(). Nothing else references it.
//
// ALSO UNVERIFIED until (1): the GraphQL *variable* signatures below were not
// captured from the app traffic (only the op names, the gateways and the
// response shapes were). Expect to correct the `query ...($x: T!)` headers
// against a live capture the first time this is enabled.
//
// AND: THERE IS NO ETA / ESTIMATED-READY FIELD. Verified 2026-08-24 across
// every datetime field on all five service ops — zero future-dated values.
// appointmentStartAtIso/appointmentEndAtIso are the DROP-OFF window and sit in
// the PAST while the car is still being worked on. Do not present either as a
// "ready by" time and do not compute one. `completedAt` going null → timestamp
// is the only completion signal Rivian gives.

// Separate vehicle-service gateway — NOT the GATEWAY constant at the top of
// this file. Same session auth headers, different host path.
const VS_GATEWAY = 'https://rivian.com/api/vs/gql-gateway';

/**
 * Master kill switch. Disabled by default; only an explicit truthy
 * RIVIAN_SERVICE_MODE env var enables the calls below.
 */
export function rivianServiceModeEnabled(): boolean {
  const v = String(process.env.RIVIAN_SERVICE_MODE ?? '').trim().toLowerCase();
  return v === '1' || v === 'true' || v === 'yes' || v === 'on';
}

export type RivianWorkOrderType = 'SERVICE_CENTER' | 'MOBILE_SERVICE' | string;
export type RivianWorkOrderStatus = 'IN_PROGRESS' | 'DELIVERED' | string;
/** OPEN_SCHEDULED → OPEN_IN_PROGRESS → CLOSED_WORK_COMPLETE */
export type RivianServiceItemStatus =
  | 'OPEN_SCHEDULED'
  | 'OPEN_IN_PROGRESS'
  | 'CLOSED_WORK_COMPLETE'
  | string;

/**
 * Map a raw Rivian service request status string to a user-facing category.
 *
 * Coerces with `String(raw ?? '').trim().toUpperCase()` so null, undefined,
 * numbers, and empty strings all normalise safely.
 *
 *   'OPEN_IN_PROGRESS'  -> 'active'
 *   'CLOSED_WORK_COMPLETE' -> 'done'
 *   'OPEN_SCHEDULED'    -> 'queued'  (falls through to default)
 *   any other / unknown  -> 'queued'
 *
 * Never throws.
 */
export function mapServiceRequestStatus(
  raw: unknown,
): 'active' | 'queued' | 'done' {
  const s = String(raw ?? '').trim().toUpperCase();
  if (s === 'OPEN_IN_PROGRESS') return 'active';
  if (s === 'CLOSED_WORK_COMPLETE') return 'done';
  return 'queued';
}

export interface RivianServiceThread {
  workOrderId: string | null;
  workOrderType: RivianWorkOrderType | null;
  workOrderStatus: RivianWorkOrderStatus | null;
  threadStatus: string | null;
  appointmentDate: string | null;
}

export interface RivianServiceLineItem {
  title: string | null;
  status: RivianServiceItemStatus | null;
  concern: string | null;
  requestType: string | null;
  referenceId: string | null;
  createdAt: string | null;
  /** Convenience flags derived from `status`; null when status is absent. */
  isComplete: boolean | null;
  isInProgress: boolean | null;
}

export interface RivianWorkOrderTiming {
  workOrderId: string;
  /** Start of the DROP-OFF window. Not an ETA. */
  appointmentStartAtIso: string | null;
  /** End of the DROP-OFF window. NOT a "ready by" time — see notes above. */
  appointmentEndAtIso: string | null;
  /** null while work is in progress; real timestamp once finished. */
  completedAt: string | null;
}

export interface RivianServiceState {
  inService: boolean;
  workOrderId: string | null;
  appointmentDate: string | null;
  threads: RivianServiceThread[];
  lineItems: RivianServiceLineItem[];
  timing: RivianWorkOrderTiming | null;
  /** e.g. 2 of 5 line items at CLOSED_WORK_COMPLETE. */
  itemsComplete: number;
  itemsTotal: number;
}

// Variable signatures UNVERIFIED — see the dormancy note above.
const GET_ASYNC_MESSAGE_THREAD_LIST = `
query GetAsyncMessageThreadList {
  commsListDiscussions {
    workOrderId
    workOrderType
    workOrderStatus
    threadStatus
    appointmentDate
  }
}`;

const GET_ACTIVE_REQUESTS = `
query GetActiveRequests($vehicleId: String!) {
  consumerServiceRequests(vehicleId: $vehicleId) {
    result {
      title
      status
      concern
      requestType
      referenceId
      createdAt
      expiresAt
    }
  }
}`;

const QUERY_BY_WORK_ORDER_ID = `
query QueryByWorkOrderId($workOrderId: String!) {
  queryByWorkOrderId(workOrderId: $workOrderId) {
    workOrderId
    appointmentStartAtIso
    appointmentEndAtIso
    visitStartAtIso
    visitEndAtIso
    completedAt
  }
}`;

// Rivian's gateway is loose about String-typed fields (it has returned bare
// JSON numbers where its own schema says String — see the 2026-08-06 gotchas
// above), and service payloads mix "field is null" with "field is absent".
// Everything below goes through these two coercions rather than trusting
// either the declared type or the field's presence.
function svcStr(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s === '' ? null : s;
}

function svcArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

interface RawServiceThread {
  workOrderId?: unknown;
  workOrderType?: unknown;
  workOrderStatus?: unknown;
  threadStatus?: unknown;
  appointmentDate?: unknown;
}

interface RawServiceRequest {
  title?: unknown;
  status?: unknown;
  concern?: unknown;
  requestType?: unknown;
  referenceId?: unknown;
  createdAt?: unknown;
}

interface RawWorkOrderTiming {
  workOrderId?: unknown;
  appointmentStartAtIso?: unknown;
  appointmentEndAtIso?: unknown;
  completedAt?: unknown;
}

function mapServiceThread(raw: RawServiceThread): RivianServiceThread {
  return {
    workOrderId: svcStr(raw?.workOrderId),
    workOrderType: svcStr(raw?.workOrderType),
    workOrderStatus: svcStr(raw?.workOrderStatus),
    threadStatus: svcStr(raw?.threadStatus),
    appointmentDate: svcStr(raw?.appointmentDate),
  };
}

function mapServiceLineItem(raw: RawServiceRequest): RivianServiceLineItem {
  const status = svcStr(raw?.status);
  return {
    title: svcStr(raw?.title),
    status,
    concern: svcStr(raw?.concern),
    requestType: svcStr(raw?.requestType),
    referenceId: svcStr(raw?.referenceId),
    createdAt: svcStr(raw?.createdAt),
    isComplete: status === null ? null : status === 'CLOSED_WORK_COMPLETE',
    isInProgress: status === null ? null : status === 'OPEN_IN_PROGRESS',
  };
}

/**
 * DORMANT. In-service detection via the main gateway.
 * Returns [] when the flag is off, when there are no tokens, or on error —
 * an empty list is indistinguishable from "shared account can't see it", so
 * callers must not treat [] as proof the car is not in service.
 */
export async function fetchRivianServiceThreads(): Promise<RivianServiceThread[]> {
  if (!rivianServiceModeEnabled()) return [];
  const tokens = readRivianTokens();
  if (!tokens) return [];
  if (inBackoffWindow()) return [];

  try {
    const data = await gql<{ commsListDiscussions?: RawServiceThread[] | null }>(
      GET_ASYNC_MESSAGE_THREAD_LIST,
      {},
      authHeaders(tokens),
    );
    return svcArray<RawServiceThread>(data?.commsListDiscussions).map(mapServiceThread);
  } catch (e) {
    // Deliberately does NOT touch the shared backoff/alert counters: this is
    // an opt-in side channel and must never degrade the vehicleState poll.
    console.warn('[rivian] fetchRivianServiceThreads failed:', String(e).slice(0, 240));
    return [];
  }
}

/**
 * DORMANT. Per-line-item service checklist, from the SEPARATE vs gateway.
 * Same caveat as above: [] does not mean "no work items".
 */
export async function fetchRivianServiceLineItems(vehicleId?: string): Promise<RivianServiceLineItem[]> {
  if (!rivianServiceModeEnabled()) return [];
  const tokens = readRivianTokens();
  if (!tokens) return [];
  const vid = vehicleId ?? tokens.vehicleId;
  if (!vid) return [];
  if (inBackoffWindow()) return [];

  try {
    const data = await gql<{ consumerServiceRequests?: { result?: RawServiceRequest[] | null } | null }>(
      GET_ACTIVE_REQUESTS,
      { vehicleId: vid },
      authHeaders(tokens),
      VS_GATEWAY,
    );
    return svcArray<RawServiceRequest>(data?.consumerServiceRequests?.result).map(mapServiceLineItem);
  } catch (e) {
    console.warn('[rivian] fetchRivianServiceLineItems failed:', String(e).slice(0, 240));
    return [];
  }
}

/**
 * DORMANT. Drop-off window + completion stamp for one work order.
 * `appointmentEndAtIso` is NOT an ETA — see the dormancy note above.
 */
export async function fetchRivianWorkOrderTiming(workOrderId: string): Promise<RivianWorkOrderTiming | null> {
  if (!rivianServiceModeEnabled()) return null;
  const wo = svcStr(workOrderId);
  if (!wo) return null;
  const tokens = readRivianTokens();
  if (!tokens) return null;
  if (inBackoffWindow()) return null;

  try {
    const data = await gql<{ queryByWorkOrderId?: RawWorkOrderTiming | null }>(
      QUERY_BY_WORK_ORDER_ID,
      { workOrderId: wo },
      authHeaders(tokens),
      VS_GATEWAY,
    );
    const raw = data?.queryByWorkOrderId;
    if (!raw) return null;
    return {
      workOrderId: svcStr(raw.workOrderId) ?? wo,
      appointmentStartAtIso: svcStr(raw.appointmentStartAtIso),
      appointmentEndAtIso: svcStr(raw.appointmentEndAtIso),
      completedAt: svcStr(raw.completedAt),
    };
  } catch (e) {
    console.warn('[rivian] fetchRivianWorkOrderTiming failed:', String(e).slice(0, 240));
    return null;
  }
}

/**
 * DORMANT aggregate: the single entry point a future caller would use.
 * Returns null unless RIVIAN_SERVICE_MODE is explicitly enabled. Nothing in
 * the app calls this today — see the dormancy note above for what flips it on.
 */
export async function fetchRivianServiceState(vehicleId?: string): Promise<RivianServiceState | null> {
  if (!rivianServiceModeEnabled()) return null;

  const threads = await fetchRivianServiceThreads();
  // In service iff ANY thread is IN_PROGRESS (SERVICE_CENTER or MOBILE_SERVICE).
  const active = threads.find((t) => t.workOrderStatus === 'IN_PROGRESS') ?? null;
  const inService = active !== null;

  const lineItems = inService ? await fetchRivianServiceLineItems(vehicleId) : [];
  const timing = active?.workOrderId ? await fetchRivianWorkOrderTiming(active.workOrderId) : null;

  const itemsTotal = lineItems.length;
  const itemsComplete = lineItems.filter((i) => i.isComplete === true).length;

  return {
    inService,
    workOrderId: active?.workOrderId ?? null,
    appointmentDate: active?.appointmentDate ?? null,
    threads,
    lineItems,
    timing,
    itemsComplete,
    itemsTotal,
  };
}

/**
 * Derive the abbreviated appointment label from a Rivian workOrderId.
 *
 * Example: 'WO-000002434913' → '#2434913'
 *
 * Coerces with String() first (may arrive as a number).  Strips every
 * non-digit character, then strips leading zeros, prefixes '#'.
 * Returns null when the input is null/undefined, has no digits, or is
 * all zeros.
 */
export function formatApptNumber(workOrderId: unknown): string | null {
  if (workOrderId == null) return null;
  const digits = String(workOrderId).replace(/\D/g, '');
  if (digits === '') return null;
  if (/^0+$/.test(digits)) return null;
  return '#' + digits.replace(/^0+/, '');
}

/**
 * Select the active (in-progress) work order from a commsListDiscussions
 * array.  Among multiple IN_PROGRESS threads, returns the one with the
 * latest appointmentDate; items with a missing or unparseable date sort
 * oldest (via -Infinity).  Returns null when threads is not an array or
 * no element has workOrderStatus === 'IN_PROGRESS'.
 */
export function pickActiveWorkOrder(
  threads: unknown,
): { workOrderId: string; workOrderType: string | null; appointmentDate: string | null } | null {
  if (!Array.isArray(threads)) return null;

  let best: { workOrderId: string; workOrderType: string | null; appointmentDate: string | null } | null = null;
  let bestDate = -Infinity;

  for (const thread of threads) {
    if (thread === null || thread === undefined || typeof thread !== 'object') continue;

    const workOrderStatus = (thread as Record<string, unknown>).workOrderStatus;
    const statusMatch = String(workOrderStatus).toUpperCase() === 'IN_PROGRESS';
    if (!statusMatch) continue;

    const raw = thread as Record<string, unknown>;
    const workOrderId = String(raw.workOrderId);
    const workOrderType = raw.workOrderType == null ? null : String(raw.workOrderType);
    const appointmentDate = raw.appointmentDate == null ? null : String(raw.appointmentDate);

    let dateVal = -Infinity;
    if (appointmentDate) {
      const d = new Date(appointmentDate);
      if (!isNaN(d.getTime())) {
        dateVal = d.getTime();
      }
    }

    if (dateVal >= bestDate) {
      bestDate = dateVal;
      best = { workOrderId, workOrderType, appointmentDate };
    }
  }

  return best;
}
