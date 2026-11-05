// Circuit breaker for a storm of valid vendor events.
//
// It counts in memory, so checking it costs nothing, and it saves its day count
// at most once a minute, or at once for a new incident.  A restart loses at most
// the few unsaved deliveries of one wake.  The writer runs one request at a time
// (AlertWriter.fetch chains them), so nothing here can interleave with itself.
//
// What a shed delivery answers is decided in handleEvent, because a vendor's
// retry is its only redelivery:  200 only when the state is already safe in
// durable storage, otherwise 503 with Retry-After.

export const SOURCE_EVENTS_PER_MINUTE = 60;
export const DELIVERIES_PER_DAY = 5000;
// A storm from one vendor must not lock the others out until the day resets, so
// each source keeps this many deliveries a day past the shared cap.  The day's
// worst case is the cap plus this for each other credentialed source.
export const SOURCE_DAY_RESERVE = 500;
// A shed transition still writes its state, and later its block, so those writes
// have their own cap.
export const SHED_WRITES_PER_DAY = 2000;
// A new incident is by far the dearest delivery, at about 18 rows written with
// its promotion against about 6 for a transition.  This cap is what keeps a
// unique-key storm inside the 100,000 rows a day the free plan allows.  It has
// the same per-source reserve as the delivery cap, for the same reason.
export const NEW_KEYS_PER_DAY = 1000;
export const SOURCE_NEW_KEY_RESERVE = 100;

const STORAGE_KEY = "breaker:v1";
const SAVE_EVERY_MS = 60_000;
// Quiet traffic never needs saving: it adds no row per delivery.  Low, because
// the object is evicted when idle and an unsaved count goes with it.
const SAVE_MIN_UNSAVED = 5;
const DAY_MS = 86_400_000;

const count = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : 0);
const counts = (value) =>
  new Map(
    Object.entries(value && typeof value === "object" ? value : {}).map(([name, n]) => [name, count(n)]),
  );

export class DeliveryBreaker {
  constructor(storage, now = Date.now) {
    this.storage = storage;
    this.now = now;
    this.loaded = false;
    this.day = Math.floor(now() / DAY_MS);
    this.total = 0;
    this.shedWrites = 0;
    this.newKeys = 0;
    this.minute = -1;
    this.perSource = new Map();
    this.perSourceDay = new Map();
    this.newBySource = new Map();
    this.shed = new Map();
    this.announced = new Set();
    this.logged = new Set();
    this.savedAt = 0;
    this.unsaved = 0;
  }

  // A read that fails throws and is tried again on the next delivery.  Starting
  // from zero instead would reopen every cap, and the next save would overwrite
  // the day's real count.  A failed read loses nothing:  the vendor gets a 503,
  // and the delivery could not have read its own state either.
  async load() {
    if (this.loaded) return;
    let saved;
    try {
      saved = await this.storage.get(STORAGE_KEY);
    } catch (cause) {
      const error = new Error("breaker state could not be read");
      error.code = "breaker_unavailable";
      error.cause = cause;
      throw error;
    }
    const day = Math.floor(this.now() / DAY_MS);
    this.day = day;
    // This worker wrote the record, so a bad one is corruption.  It starts the
    // day over rather than refusing every delivery until someone deletes a row.
    if (saved !== undefined && (saved === null || typeof saved !== "object")) {
      console.error(JSON.stringify({ event: "alert.breaker_state_invalid" }));
    } else if (saved?.day === day) {
      this.total = count(saved.total);
      this.shedWrites = count(saved.shedWrites);
      this.newKeys = count(saved.newKeys);
      this.perSourceDay = counts(saved.sources);
      this.newBySource = counts(saved.newBySource);
      this.shed = counts(saved.shed);
    }
    this.loaded = true;
  }

  roll(at) {
    const minute = Math.floor(at / 60_000);
    if (minute !== this.minute) {
      this.minute = minute;
      this.perSource.clear();
      this.announced.clear();
      this.logged.clear();
    }
    const day = Math.floor(at / DAY_MS);
    if (day !== this.day) {
      this.day = day;
      this.total = 0;
      this.shedWrites = 0;
      this.newKeys = 0;
      this.perSourceDay.clear();
      this.newBySource.clear();
      this.shed.clear();
      this.unsaved = 0;
    }
  }

  // Count one authenticated, well-formed delivery and say whether to shed it.
  async admit(source) {
    await this.load();
    const at = this.now();
    this.roll(at);
    this.total++;
    this.unsaved++;
    const perMinute = (this.perSource.get(source) || 0) + 1;
    this.perSource.set(source, perMinute);
    const today = (this.perSourceDay.get(source) || 0) + 1;
    this.perSourceDay.set(source, today);
    const reason =
      this.total > DELIVERIES_PER_DAY && today > SOURCE_DAY_RESERVE
        ? "day"
        : perMinute > SOURCE_EVENTS_PER_MINUTE
          ? "minute"
          : null;
    const notice = `${reason}:${source}`;
    const trippedNow = reason !== null && !this.announced.has(notice);
    if (trippedNow) this.announced.add(notice);
    await this.save(at);
    return {
      shed: reason !== null,
      reason,
      trippedNow,
      retryAfter: reason === "day" ? secondsToNextDay(at) : 60,
    };
  }

  // Reserve one shed state write.  False means the day's allowance is gone.
  async spendShedWrite() {
    if (this.shedWrites >= SHED_WRITES_PER_DAY) return false;
    this.shedWrites++;
    this.unsaved++;
    return true;
  }

  // Reserve one new incident.  False means the day's allowance is gone.  The
  // count is saved at once:  a new incident already writes many rows, and a burst
  // followed by an eviction must not be able to spend the same allowance twice.
  async spendNewKey(source) {
    const at = this.now();
    this.roll(at);
    const today = this.newBySource.get(source) || 0;
    if (this.newKeys >= NEW_KEYS_PER_DAY && today >= SOURCE_NEW_KEY_RESERVE) return false;
    this.newKeys++;
    this.newBySource.set(source, today + 1);
    this.unsaved++;
    await this.write(at);
    return true;
  }

  // Count one delivery that was shed or refused.  The count is durable, and
  // `log` is true at most once a minute per reason and source, because a storm
  // must not also run up the log volume.  Each line carries the day's count, so
  // a skipped line loses nothing.
  noteShed(source, reason) {
    const at = this.now();
    this.roll(at);
    const name = `${reason}:${source}`;
    const today = (this.shed.get(name) || 0) + 1;
    this.shed.set(name, today);
    this.unsaved++;
    const log = !this.logged.has(name);
    if (log) this.logged.add(name);
    return { today, log };
  }

  // The day's counts, for /status.
  async snapshot() {
    await this.load();
    this.roll(this.now());
    return {
      deliveries: this.total,
      newKeys: this.newKeys,
      shedWrites: this.shedWrites,
      shed: Object.fromEntries(this.shed),
    };
  }

  async save(at) {
    if (this.unsaved < SAVE_MIN_UNSAVED || at - this.savedAt < SAVE_EVERY_MS) return;
    await this.write(at);
  }

  async write(at) {
    try {
      await this.storage.put(STORAGE_KEY, {
        day: this.day,
        total: this.total,
        shedWrites: this.shedWrites,
        newKeys: this.newKeys,
        sources: Object.fromEntries(this.perSourceDay),
        newBySource: Object.fromEntries(this.newBySource),
        shed: Object.fromEntries(this.shed),
      });
      this.savedAt = at;
      this.unsaved = 0;
    } catch {
      // The count stays in memory and goes out with a later delivery.
    }
  }
}

// At most an hour, so a retrying vendor still comes back within its own window.
export function secondsToNextDay(at) {
  const remaining = Math.ceil((DAY_MS - (at % DAY_MS)) / 1000);
  return Math.min(remaining, 3600);
}
