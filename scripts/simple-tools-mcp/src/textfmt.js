// The outbound text fix-ups:  a port of scripts/simple_tools/textfmt.py (AFC #392
// and owner 2026-10-19), so a hosted post carries the same text a stdio post does.
//
// After `.`, `!` or `?` (optionally followed by closing characters " ' ” ’ ) ] * _),
// two or more ASCII spaces before a non-space character on the same line become
// exactly U+00A0 plus one ASCII space.  Fenced code blocks, inline code spans,
// `$$` math, @-mentions and list markers such as `1.  item` are left alone.  It
// is idempotent and never lengthens the text, so the secret scan and the
// 10,000-character cap reach the same verdict before and after.
//
// `singleGaps` widens a ONE space after a sentence (agents type one, and some
// tools collapse two into one before the text reaches the client) and
// `fleetDates` rewrites an ISO date in prose (`2026-10-19`, `2026-10-20T02:03:00Z`)
// into the fleet form (`Mon, Oct 19`, `Mon, Oct 19, 9:03pm`), in Central, with the
// year only when it is not this year.  A date inside a name or a path
// (`backup-2026-10-19.tar`, `/2026-10-19/`) is left alone.  Both skip code, math and
// mentions, and both report what they changed so the caller can tell the author.
//
// `fixOutbound` is the one rule both transports apply:  call it wherever
// sentenceGap was applied alone.
//
// test/textfmt.test.mjs runs the Python suite's own tables against this port.

export const NBSP = " ";
export const GAP = `${NBSP} `;

// [ ] and not \s:  NBSP is whitespace to \s, and matching it would stop an
// existing gap from being left exactly as it is.
const GAP_RE = /([.!?]["'”’)\]*_]*)( {2,})(?=[^\s])/g;
const LIST_MARKER_PREFIX_RE = /^[ \t>]*\d{1,9}$/;

// The info string is [^\n]*, not .*:  JS `.` stops at \r and U+2028, Python's does not.
const FENCE_OPEN_RE = /^((?:[ \t]*(?:>|[-+*]|\d{1,9}[.)]))*[ \t]*)(`{3,}|~{3,})([^\n]*)$/;
const FENCE_CLOSE_RE = /^[ \t]*(?:>[ \t]*)*(`{3,}|~{3,})[ \t\r]*$/;

// Inline spans, left to right so the earlier opener wins.  None may run across
// a blank line, and an opener with no closer protects nothing.
const INLINE_RE = new RegExp(
  "((?<!`)(`+)(?!`)(?:(?!\\n[ \\t]*\\n)[\\s\\S])+?(?<!`)\\2(?!`))" +
    "|(\\$\\$(?:(?!\\n[ \\t]*\\n)[\\s\\S])+?\\$\\$)" +
    "|(@_?\\*{1,2}[^*\\n]+\\*{1,2})",
  "g",
);

const MASK = "";

/** [fenced, prose] line-aligned [start, end) ranges of `text`. */
function splitFences(text) {
  const fenced = [];
  const prose = [];
  let pos = 0;
  let proseStart = 0;
  let fenceStart = 0;
  let fenceChar = "";
  let fenceLen = 0;
  for (const line of text.split("\n")) {
    const end = pos + line.length + 1;
    if (fenceChar) {
      const closer = FENCE_CLOSE_RE.exec(line);
      if (closer && closer[1][0] === fenceChar && closer[1].length >= fenceLen) {
        fenced.push([fenceStart, Math.min(end, text.length)]);
        fenceChar = "";
        proseStart = Math.min(end, text.length);
      }
    } else {
      const opener = FENCE_OPEN_RE.exec(line);
      if (opener && !(opener[2][0] === "`" && opener[3].includes("`"))) {
        if (proseStart < pos) prose.push([proseStart, pos]);
        fenceStart = pos;
        fenceChar = opener[2][0];
        fenceLen = opener[2].length;
      }
    }
    pos = end;
  }
  if (fenceChar) fenced.push([fenceStart, text.length]);
  else if (proseStart < text.length) prose.push([proseStart, text.length]);
  return [fenced, prose];
}

function protectedRanges(text) {
  const [fenced, prose] = splitFences(text);
  const ranges = [...fenced];
  for (const [start, end] of prose) {
    const chunk = text.slice(start, end);
    INLINE_RE.lastIndex = 0;
    for (const match of chunk.matchAll(INLINE_RE)) ranges.push([start + match.index, start + match.index + match[0].length]);
  }
  return ranges;
}

/** `text` with every protected range (fences, code spans, math, mentions) masked out, length unchanged. */
// Mask by UTF-16 index:  every range above is in UTF-16 units, and the mask
// keeps the length, so match indexes line up with `text`.
function maskedText(text, ranges = protectedRanges(text)) {
  const masked = text.split("");
  for (const [start, end] of ranges) {
    for (let i = start; i < end; i++) {
      if (masked[i] !== "\n" && masked[i] !== "\r") masked[i] = MASK;
    }
  }
  return masked.join("");
}

/** `text` with each ASCII-space sentence gap turned into U+00A0 plus one space. */
export function sentenceGap(text) {
  if (typeof text !== "string" || !text.includes("  ")) return text;
  const masked = maskedText(text);
  const pieces = [];
  let last = 0;
  GAP_RE.lastIndex = 0;
  for (const match of masked.matchAll(GAP_RE)) {
    const spacesStart = match.index + match[1].length;
    if (match[1] === ".") {
      const lineStart = text.lastIndexOf("\n", match.index - 1) + 1;
      if (LIST_MARKER_PREFIX_RE.test(text.slice(lineStart, match.index))) continue;
    }
    pieces.push(text.slice(last, spacesStart), GAP);
    last = spacesStart + match[2].length;
  }
  if (pieces.length === 0) return text;
  pieces.push(text.slice(last));
  return pieces.join("");
}

// ---- Owner 2026-10-19:  the gap and the fleet date format, and what they changed ------------------------------
//
// sentenceGap only widens a gap the author typed as two spaces.  Agents often type one (and some tools collapse
// two into one before the text reaches the client), so singleGaps also widens ONE space after a sentence when a
// capital letter, digit, quote, `*`, `#`, `(`, `[` (a markdown link opener), code or a mention follows.  It is a heuristic:  a list marker
// (`1. Item`) is skipped, and so are known abbreviations, dotted words and single-letter initials (`Dr. Smith`,
// `U.S. Army`, `J. Doe`).  A missed gap is the old behaviour;  a wrong one is a slightly wider space, never changed
// words.
//
// fleetDates rewrites an ISO date in prose (`2026-10-19`, `2026-10-20T02:03:00Z`) into the fleet form
// (`Mon, Oct 19`, `Mon, Oct 19, 9:03pm`), in Central, with the year only when it is not this year.  A date inside a
// name or a path (`backup-2026-10-19.tar`, `/2026-10-19/`) is left alone.
//
// Both skip code, math and mentions (the same protected ranges as sentenceGap), and both report what they changed,
// so the caller can tell the author.

// "no" is absent on purpose:  "I said no.  Then we stop." ends a sentence, and "No. 5" cannot be told apart from it
// here, so the same call textfmt.py and check-routine-prompts.py make.  A wrong widening is a slightly wider space.
const ABBREVIATIONS = new Set([
  "mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "eg", "ie", "cf", "al", "approx", "dept",
  "est", "fig", "nos", "vol", "ca", "mt", "ft", "sq",
]);

// The lookahead carries MASK as well as the plain characters:  a one-space gap before a code span or a mention
// is still a gap, and masking is what lets that stay true (textfmt.py's class holds U+E000 for the same reason).
const SINGLE_GAP_RE = /([.!?]["'”’)\]*_]*)( )(?=[A-Z0-9"“(*#\[])/g;
// Python's `$` also matches before one trailing newline;  singleGaps strips it rather than emulate it.
const WORD_BEFORE_RE = /([A-Za-z][A-Za-z.]*)$/;
// A linear pre-check (AFC-722 review):  the old /[.!?]\S* / rescanned a long space-free run from every dot.
const mayHaveSentenceEnd = (text) => text.includes(" ") && /[.!?]/.test(text);

// The same rules as the Apple Notes script's date rewrite.  A date is a token of its own:  a name or a query string
// that merely contains one keeps it, and a dot after the date ends a sentence unless a word character follows.  On
// the space form a -HH:MM is the end of a time range (2026-10-19 10:30-11:00), so it is tried first;  it is read as
// a UTC offset only when the start carried seconds and the end did not (2026-10-19 10:30:00-05:00), because a
// range that shows seconds shows them on both ends (2026-10-19 10:30:00-11:00:00).  A +HH:MM is never a range.
// The word class is spelled out as \p{L}\p{N}_ rather than \w because JavaScript's \w is ASCII-only and Python's is
// not:  with \w here, "café2026-10-19" would be rewritten by this port and kept by textfmt.py.
const ISO_RE =
  /(?<![\p{L}\p{N}_/.:=?&-])(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?| (\d{2}):(\d{2})(:\d{2}(?:\.\d+)?)?(?:-(\d{2}):(\d{2})(:\d{2}(?:\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?)?(?![\p{L}\p{N}_/-]|\.[A-Za-z0-9])/gu;
const ANY_ISO_RE = /\d{4}-\d{2}-\d{2}/;

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// America/Chicago wall-clock time, so a UTC stamp reads as the fleet's local time.  Falls back to the US rule
// (second Sunday of March to first Sunday of November) where the runtime has no tz database, the same fallback
// textfmt.py's _central applies.
let centralFormatter = null;
try {
  centralFormatter = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
} catch {
  centralFormatter = null;
}

/** [year, month, day, hour, minute] for the instant `ms` in Central wall-clock time (month is 1-based). */
function centralFrom(ms) {
  if (centralFormatter) {
    const parts = {};
    for (const { type, value } of centralFormatter.formatToParts(new Date(ms))) {
      if (type !== "literal") parts[type] = Number(value);
    }
    // h23 can still render midnight as 24 on some runtimes.
    return { y: parts.year, m: parts.month, d: parts.day, h: parts.hour % 24, mi: parts.minute };
  }
  const at = new Date(ms);
  const year = at.getUTCFullYear();
  const nthSunday = (month, n) => {
    const first = new Date(0);
    first.setUTCFullYear(year, month - 1, 1);
    // getUTCDay() is Sunday=0, so the first Sunday is day 1 + ((7 - weekday) % 7).  (textfmt.py uses Python's
    // Monday=0 base, which is where the 6 comes from there.)
    return 1 + ((7 - first.getUTCDay()) % 7) + 7 * (n - 1);
  };
  const start = utcMs(year, 2, nthSunday(3, 2), 8);
  const end = utcMs(year, 10, nthSunday(11, 1), 7);
  const shifted = new Date(ms + (start <= ms && ms < end ? -5 : -6) * 3600000);
  return {
    y: shifted.getUTCFullYear(),
    m: shifted.getUTCMonth() + 1,
    d: shifted.getUTCDate(),
    h: shifted.getUTCHours(),
    mi: shifted.getUTCMinutes(),
  };
}

/** Date.UTC that keeps a two-digit year as written (Date.UTC would read it as 19xx). */
function utcMs(year, month, day, hour = 0, minute = 0) {
  const at = new Date(0);
  at.setUTCFullYear(year, month, day);
  at.setUTCHours(hour, minute, 0, 0);
  return at.getTime();
}

/** The day of the week for a plain date, without a tz. */
function weekdayOf(year, month, day) {
  const at = new Date(0);
  at.setUTCFullYear(year, month - 1, day);
  return at.getUTCDay();
}

function daysInMonth(year, month) {
  const at = new Date(0);
  at.setUTCFullYear(year, month, 0);
  return at.getUTCDate();
}

function validDate(year, month, day) {
  return month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth(year, month);
}

/** `Mon, Oct 19`, plus the year when it is not this year. */
function fleetDay(when, thisYear) {
  return (
    `${WEEKDAYS[weekdayOf(when.y, when.m, when.d)]}, ${MONTHS[when.m - 1]} ${when.d}` +
    (when.y !== thisYear ? `, ${when.y}` : "")
  );
}

/** `9:03am`. */
function clock(hour, minute) {
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")}${hour < 12 ? "am" : "pm"}`;
}

/** Minutes east of UTC for `Z`, `+HH:MM`, `+HHMM` or `-HH:MM`. */
function offsetMinutes(zone) {
  if (zone === "Z") return 0;
  const sign = zone[0] === "-" ? -1 : 1;
  const digits = zone.slice(1).replace(":", "");
  return sign * (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2, 4)));
}

/** [text with each one-space sentence gap widened to U+00A0 plus a space, how many were widened]. */
export function singleGaps(text) {
  if (typeof text !== "string" || !mayHaveSentenceEnd(text)) return [text, 0];
  const masked = maskedText(text);
  const pieces = [];
  let last = 0;
  let count = 0;
  SINGLE_GAP_RE.lastIndex = 0;
  for (const match of masked.matchAll(SINGLE_GAP_RE)) {
    const window = masked.slice(Math.max(0, match.index - 40), match.index);
    const before = WORD_BEFORE_RE.exec(window.endsWith("\n") ? window.slice(0, -1) : window);
    const word = before ? before[1].toLowerCase().replace(/\.+$/, "") : "";
    if (match[1].startsWith(".") && before && (ABBREVIATIONS.has(word) || word.length === 1 || word.includes("."))) {
      continue;
    }
    if (match[1] === ".") {
      const lineStart = text.lastIndexOf("\n", match.index - 1) + 1;
      if (LIST_MARKER_PREFIX_RE.test(text.slice(lineStart, match.index))) continue; // `1. Item`:  U+00A0 would end the list
    }
    const spaceStart = match.index + match[1].length;
    pieces.push(text.slice(last, spaceStart), GAP);
    last = spaceStart + 1;
    count += 1;
  }
  if (count === 0) return [text, 0];
  pieces.push(text.slice(last));
  return [pieces.join(""), count];
}

// ---- AFC-722:  the house gap for hosted GitHub and Linear text -----------------------------------------------
//
// A model cannot type U+00A0 into a tool argument:  it reaches the client as a plain space (byte evidence in
// AFC-722).  So the server writes the gap itself, with the same reach as the house perl converter and its
// widened cases (the sentence-gap converter holes):  after `.`, `!` or `?`, with any closing quote, bracket,
// `*`, `_`, `~` or a Markdown link target `](url)` between, ONE OR MORE ASCII spaces before a capital, a
// lowercase letter, a digit, a quote, `*`, `#`, `@`, `(`, `[`, `_`, `~` or a code span become U+00A0 plus one
// space.  A digit before the period is a sentence end too ("must print 0.  The"), except a list marker such as
// `1. Item` at the start of a line.  An `&nbsp;` entity typed after a sentence (the Code-tab habit) becomes
// the real character, since GitHub and Linear show the entity as text.  Code spans, fenced code, `$$` math and
// mentions are never touched, and neither are abbreviations, dotted words and initials (`e.g. this`,
// `i.e. that`, `v1.2. next` keeps its gap after the version only because a sentence ends there).  Text that
// already carries U+00A0 plus a space is left exactly as it is, so a second pass changes nothing.

const HOUSE_CLOSERS = "(?:\\]\\([^()\\s]{1,255}\\)|[\"'”’)\\]*_~`])*";
const HOUSE_NEXT = `(?=[A-Za-z0-9"“‘'(*#@\\[_~\`${MASK}])`;
const HOUSE_GAP_RE = new RegExp(`([.!?]${HOUSE_CLOSERS})( +)${HOUSE_NEXT}`, "g");
const HOUSE_ENTITY_RE = new RegExp(`([.!?]${HOUSE_CLOSERS})&nbsp; ?${HOUSE_NEXT}`, "g");

// A list marker after a bullet, a task box or a heading mark (`- 1. First`, `- [ ] 1. Do it`, `## 1. Intro`) is
// still a list marker or a heading number:  U+00A0 after it would end the list or change the heading's anchor.
const HOUSE_LIST_MARKER_PREFIX_RE = /^[ \t>]*(?:[-+*][ \t]+(?:\[[ xX]\][ \t]+)?|#{1,6}[ \t]+)?\d{1,9}$/;

// The house veto list adds month and company abbreviations to the shared one (`Jan. 5`, `Acme Inc. Corp.`).  "no"
// and "pp" veto only before a digit (`No. 5`, `pp. 4`), since "I said no. Then" ends a sentence.  The shared
// ABBREVIATIONS set stays as it is, because the Zulip rule must keep matching textfmt.py.
const HOUSE_ABBREVIATIONS = new Set([
  ...ABBREVIATIONS, "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "inc",
  "corp", "co", "ltd", "llc", "bros",
]);
const HOUSE_BEFORE_DIGIT = new Set(["no", "pp", "nr"]);

// Above this many UTF-16 units the text is sent as written:  a bound on Worker CPU whatever the input.
export const HOUSE_GAP_MAX = 262144;

// Indented code (four spaces or a tab, after a blank line, outside a list) and HTML code or comments are code as
// much as a fence is:  a sentence end inside them is a command, a regex or a machine-read marker, never prose.
const INDENTED_RE = /^(?: {4}|[ ]{0,3}\t)/;
const LIST_ITEM_RE = /^[ \t>]*(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)/;
const BLANK_RE = /^[ \t\r]*$/;
const HTML_CODE_TAGS = ["pre", "code", "kbd", "samp", "tt"];

/** [start, end) ranges of indented code blocks inside the prose range [start, end). */
function indentedCodeRanges(text, start, end, out) {
  let pos = start;
  let prevBlank = true;
  let inList = false;
  let codeStart = -1;
  for (const line of text.slice(start, end).split("\n")) {
    const blank = BLANK_RE.test(line);
    const indented = !blank && INDENTED_RE.test(line);
    if (codeStart >= 0 && !blank && !indented) {
      out.push([codeStart, pos]);
      codeStart = -1;
    }
    if (codeStart < 0 && !blank) {
      if (indented && prevBlank && !inList) codeStart = pos;
      else if (!indented) inList = LIST_ITEM_RE.test(line) || (inList && !prevBlank);
    }
    prevBlank = blank;
    pos += line.length + 1;
  }
  if (codeStart >= 0) out.push([codeStart, end]);
}

/** [start, end) ranges of HTML comments and <pre>, <code>, <kbd>, <samp>, <tt> elements inside [start, end). */
function htmlCodeRanges(text, start, end, out) {
  const lower = text.slice(start, end).toLowerCase();
  // indexOf from a moving start, so each scan is linear in the chunk.
  for (let from = 0; ;) {
    const open = lower.indexOf("<!--", from);
    if (open < 0) break;
    const close = lower.indexOf("-->", open + 4);
    // An unclosed comment hides the rest of the text on GitHub, so it protects the rest.
    out.push([start + open, close < 0 ? end : start + close + 3]);
    if (close < 0) break;
    from = close + 3;
  }
  for (const tag of HTML_CODE_TAGS) {
    for (let from = 0; ;) {
      const open = lower.indexOf(`<${tag}`, from);
      if (open < 0) break;
      const next = lower[open + tag.length + 1];
      if (next !== ">" && next !== "/" && !/\s/.test(next ?? "")) {
        from = open + 1;
        continue;
      }
      const close = lower.indexOf(`</${tag}>`, open);
      if (close < 0) break;
      out.push([start + open, start + close + tag.length + 3]);
      from = close + tag.length + 3;
    }
  }
}

/** protectedRanges plus indented code and HTML code and comments:  what houseGap never rewrites. */
function houseProtectedRanges(text) {
  const ranges = protectedRanges(text);
  for (const [start, end] of splitFences(text)[1]) {
    indentedCodeRanges(text, start, end, ranges);
    htmlCodeRanges(text, start, end, ranges);
  }
  return ranges;
}

/** Whether houseGap has anything to do:  a linear check, never a backtracking regex (AFC-722 review). */
function mayNeedHouseGap(text) {
  return text.includes("&nbsp;") || mayHaveSentenceEnd(text);
}

/** A lookup of the start of the line holding `index`, linear over ascending indexes. */
function lineStarts(source) {
  let scanned = 0;
  let start = 0;
  return (index) => {
    if (index < scanned) return source.lastIndexOf("\n", index - 1) + 1;
    for (let i = scanned; i < index; i++) if (source.charCodeAt(i) === 10) start = i + 1;
    scanned = index;
    return start;
  };
}

/** `text` with every sentence gap written as U+00A0 plus one space (the house rule, AFC-722). */
export function houseGap(text) {
  if (typeof text !== "string" || text.length > HOUSE_GAP_MAX || !mayNeedHouseGap(text)) return text;
  // The entity first, on its own pass:  it is not a space run, and it shortens the text, so the second pass
  // works on fresh indexes.
  let out = rewriteGaps(text, HOUSE_ENTITY_RE, () => true);
  let lineStart = null;
  out = rewriteGaps(out, HOUSE_GAP_RE, (masked, match, source) => {
    lineStart ??= lineStarts(source);
    // Two or more spaces are the author's own gap;  only a single space is a guess that the abbreviation and
    // initial lists can veto.
    if (match[1].startsWith(".") && match[2].length === 1) {
      const nextChar = masked[match.index + match[0].length] ?? "";
      // An ellipsis before a lowercase word is a pause, not a sentence end (`Hmm... maybe`).
      if (masked[match.index - 1] === "." && /\p{Ll}/u.test(nextChar)) return false;
      const window = masked.slice(Math.max(0, match.index - 40), match.index);
      const before = WORD_BEFORE_RE.exec(window);
      const word = before ? before[1].toLowerCase().replace(/\.+$/, "") : "";
      // The tail of a path, URL or identifier (`https://x.test/a.`, `v2_b.`) is not an abbreviation or an
      // initial:  only a word that stands on its own is.
      const lead = before ? window[window.length - before[1].length - 1] : undefined;
      const standalone = lead === undefined || !/[\p{L}\p{N}_/\\:=@#%&-]/u.test(lead);
      if (before && standalone && (HOUSE_ABBREVIATIONS.has(word) || word.length === 1 || word.includes("."))) return false;
      if (before && standalone && HOUSE_BEFORE_DIGIT.has(word) && /[0-9]/.test(nextChar)) return false;
    }
    if (match[1] === ".") {
      const start = lineStart(match.index);
      if (HOUSE_LIST_MARKER_PREFIX_RE.test(source.slice(start, match.index))) return false; // `1. Item` stays a list
    }
    return true;
  });
  return out;
}

/** Each unprotected `re` match in `text` whose `keep` says so becomes its group 1 plus GAP. */
function rewriteGaps(text, re, keep) {
  const masked = maskedText(text, houseProtectedRanges(text));
  const pieces = [];
  let last = 0;
  re.lastIndex = 0;
  for (const match of masked.matchAll(re)) {
    if (!keep(masked, match, text)) continue;
    const end = match.index + match[0].length;
    pieces.push(text.slice(last, match.index), text.slice(match.index, match.index + match[1].length), GAP);
    last = end;
  }
  if (pieces.length === 0) return text;
  pieces.push(text.slice(last));
  return pieces.join("");
}

/** [text with ISO dates in prose in the fleet form, [[old, new], ...]]. */
export function fleetDates(text, now = undefined) {
  if (typeof text !== "string" || !ANY_ISO_RE.test(text)) return [text, []];
  const thisYear = centralFrom(now === undefined ? Date.now() : +now).y;
  const masked = maskedText(text);
  const pieces = [];
  const changes = [];
  let last = 0;
  ISO_RE.lastIndex = 0;
  for (const match of masked.matchAll(ISO_RE)) {
    const replacement = fleetDate(match, thisYear);
    if (replacement === match[0]) continue;
    pieces.push(text.slice(last, match.index), replacement);
    changes.push([match[0], replacement]);
    last = match.index + match[0].length;
  }
  if (changes.length === 0) return [text, []];
  pieces.push(text.slice(last));
  return [pieces.join(""), changes];
}

/** `match`'s date in the fleet form, or `match[0]` unchanged when the fields are not a real date. */
function fleetDate(match, thisYear) {
  const [year, month, day, tHour, tMinute, tZone, hour, minute, sSecs, rHour, rMinute, rSecs, zone] = match.slice(1);
  let h = hour;
  let mi = minute;
  let tz = zone;
  let rangeHour = rHour;
  let rangeMinute = rMinute;
  if (tHour !== undefined) {
    h = tHour;
    mi = tMinute;
    tz = tZone;
  } else if (rHour !== undefined && sSecs !== undefined && rSecs === undefined) {
    // Seconds on the start of a range but not on its end:  a UTC offset, not a range (see ISO_RE).
    tz = `-${rHour}:${rMinute}`;
    rangeHour = undefined;
    rangeMinute = undefined;
  }
  const Y = Number(year);
  const M = Number(month);
  const D = Number(day);
  if (!validDate(Y, M, D)) return match[0];
  if (h === undefined) return fleetDay({ y: Y, m: M, d: D }, thisYear);
  const H = Number(h);
  const Mi = Number(mi);
  if (H > 23 || Mi > 59) return match[0];
  // Without a zone the wall-clock time stands as written;  with one, read it as Central.
  const when = tz ? centralFrom(utcMs(Y, M - 1, D, H, Mi) - offsetMinutes(tz) * 60000) : { y: Y, m: M, d: D, h: H, mi: Mi };
  let out = fleetDay(when, thisYear) + ", " + clock(when.h, when.mi);
  if (rangeHour !== undefined) {
    const RH = Number(rangeHour);
    const RM = Number(rangeMinute);
    if (RH > 23 || RM > 59) return match[0];
    // The end of a range carries the same zone as its start, so it converts the same way.
    const end = tz ? centralFrom(utcMs(Y, M - 1, D, RH, RM) - offsetMinutes(tz) * 60000) : { h: RH, mi: RM };
    out += "-" + clock(end.h, end.mi);
  }
  return out;
}

/**
 * Every outbound fix, and a short report of what changed for the author.  With `limit`, a result longer than it
 * keeps only the length-safe double-space conversion (a single gap and a date each add a character).
 *
 * Idempotent, because the hosted transport applies this after the body layer already applied it:  the date rewrite
 * runs before the gap widening, so a stamp that ends in a single letter (`...T02:03:00Z.`) is decided on the same
 * text both passes see, and a second pass widens nothing new.
 */
export function fixOutbound(text, { limit = null, now = undefined } = {}) {
  const gapped = sentenceGap(text);
  const [dated, dates] = fleetDates(gapped, now);
  const [fixed, singles] = singleGaps(dated);
  if (limit !== null && [...fixed].length > limit) return [gapped, []];
  const report = [];
  if (dates.length) {
    const shown =
      dates.slice(0, 5).map(([old, next]) => `${old} to ${next}`).join(", ") +
      (dates.length > 5 ? " and more" : "");
    report.push(`dates rewritten in the fleet format (${shown});  write dates that way`);
  }
  if (singles) {
    report.push(
      `${singles} sentence gap${singles === 1 ? "" : "s"} had one space and ${singles === 1 ? "was" : "were"} widened;  put two spaces after a sentence`,
    );
  }
  return [fixed, report];
}
