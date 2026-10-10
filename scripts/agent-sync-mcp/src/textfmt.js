// The outbound sentence-gap safety net:  a port of scripts/agent_sync/textfmt.py
// (AFC #392), so a hosted post carries the same text a stdio post does.
//
// After `.`, `!` or `?` (optionally followed by closing characters " ' ” ’ ) ] * _),
// two or more ASCII spaces before a non-space character on the same line become
// exactly U+00A0 plus one ASCII space.  Fenced code blocks, inline code spans,
// `$$` math, @-mentions and list markers such as `1.  item` are left alone.  It
// is idempotent and never lengthens the text, so the secret scan and the
// 10,000-character cap reach the same verdict before and after.
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

/** `text` with each ASCII-space sentence gap turned into U+00A0 plus one space. */
export function sentenceGap(text) {
  if (typeof text !== "string" || !text.includes("  ")) return text;
  // Mask by UTF-16 index:  every range above is in UTF-16 units, and the mask
  // keeps the length, so match indexes line up with `text`.
  const masked = text.split("");
  for (const [start, end] of protectedRanges(text)) {
    for (let i = start; i < end; i++) {
      if (masked[i] !== "\n" && masked[i] !== "\r") masked[i] = MASK;
    }
  }
  const pieces = [];
  let last = 0;
  GAP_RE.lastIndex = 0;
  for (const match of masked.join("").matchAll(GAP_RE)) {
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
