// Circuit breaker for a storm of valid vendor events.
//
// It counts in memory, so checking it costs nothing, and it saves its day count
// at most once a minute.  A restart loses at most the unsaved part of the count,
// which only matters far below the caps.
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
// A shed transition still writes its state, so those writes have their own cap.
export const SHED_WRITES_PER_DAY = 5000;

const STORAGE_KEY = "breaker:v1";
const SAVE_EVERY_MS = 60_000;
// Quiet traffic never needs saving: it adds no row per delivery.
const SAVE_MIN_UNSAVED = 25;
const DAY_MS = 86_400_000;

export class DeliveryBreaker {
  constructor(storage, now = Date.now) {
    this.storage = storage;
    this.now = now;
    this.loaded = false;
    this.day = Math.floor(now() / DAY_MS);
    this.total = 0;
    this.shedWrites = 0;
    this.minute = -1;
    this.perSource = new Map();
    this.perSourceDay = new Map();
    this.announced = new Set();
    this.savedAt = 0;
    this.unsaved = 0;
  }

  async load() {
    if (this.loaded) return;
    this.loaded = true;
    try {
      const saved = await this.storage.get(STORAGE_KEY);
      if (saved?.day === this.day) {
        this.total = saved.total || 0;
        this.shedWrites = saved.shedWrites || 0;
        this.perSourceDay = new Map(Object.entries(saved.sources || {}));
      }
    } catch {
      // A breaker that cannot read its count starts from zero rather than
      // taking alert delivery down with it.
    }
  }

  roll(at) {
    const minute = Math.floor(at / 60_000);
    if (minute !== this.minute) {
      this.minute = minute;
      this.perSource.clear();
      this.announced.clear();
    }
    const day = Math.floor(at / DAY_MS);
    if (day !== this.day) {
      this.day = day;
      this.total = 0;
      this.shedWrites = 0;
      this.perSourceDay.clear();
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
    const count = (this.perSource.get(source) || 0) + 1;
    this.perSource.set(source, count);
    const today = (this.perSourceDay.get(source) || 0) + 1;
    this.perSourceDay.set(source, today);
    const reason =
      this.total > DELIVERIES_PER_DAY && today > SOURCE_DAY_RESERVE
        ? "day"
        : count > SOURCE_EVENTS_PER_MINUTE
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

  async save(at) {
    if (this.unsaved < SAVE_MIN_UNSAVED || at - this.savedAt < SAVE_EVERY_MS) return;
    try {
      await this.storage.put(STORAGE_KEY, {
        day: this.day,
        total: this.total,
        shedWrites: this.shedWrites,
        sources: Object.fromEntries(this.perSourceDay),
      });
      this.savedAt = at;
      this.unsaved = 0;
    } catch {
      // Retried on a later delivery.
    }
  }
}

// At most an hour, so a retrying vendor still comes back within its own window.
function secondsToNextDay(at) {
  const remaining = Math.ceil((DAY_MS - (at % DAY_MS)) / 1000);
  return Math.min(remaining, 3600);
}
