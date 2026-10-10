// Pure helpers for the Kody /defer flow.  No I/O.

export const REFUSAL_MESSAGE =
  "Critical findings can't be deferred.  Fix this in the PR.";

export const MANUAL_RESOLVE_MESSAGE =
  'Deferred to #N.  This thread needs a manual resolve (the workflow token could not resolve it).';

/** Hidden marker on workflow `Deferred to #N` replies so idempotency ignores spoof comments. */
export const DEFERRED_REPLY_MARKER = '<!-- kody-defer-reply -->';

export function deferredReplyBody(issueNumber) {
  return `Deferred to #${issueNumber}.  ${DEFERRED_REPLY_MARKER}`;
}

export const LABELS = {
  deferred: {
    name: 'kody-deferred',
    color: 'D4A72C',
    description: 'Agreed Kody finding, fix later',
  },
  critical: {
    name: 'kody-critical',
    color: 'B60205',
    description: 'Kody finding at critical severity',
  },
  high: {
    name: 'kody-high',
    color: 'D93F0B',
    description: 'Kody finding at high severity',
  },
  medium: {
    name: 'kody-medium',
    color: 'FBCA04',
    description: 'Kody finding at medium severity',
  },
  low: {
    name: 'kody-low',
    color: 'C5DEF5',
    description: 'Kody finding at low severity',
  },
};

const SEVERITIES = ['critical', 'high', 'medium', 'low'];

/** Capitalized refusal for a blocked severity.  Two spaces after the period. */
export function refusalMessage(severity = 'critical') {
  const raw = String(severity || 'critical').toLowerCase();
  const name = raw.charAt(0).toUpperCase() + raw.slice(1);
  return `${name} findings can't be deferred.  Fix this in the PR.`;
}

/**
 * Second reply when resolveReviewThread fails.
 * Two spaces after the period.
 */
export function manualResolveReply(issueNumber, error) {
  const short = String(error || 'unknown error').replace(/\s+/g, ' ').slice(0, 180);
  const number = issueNumber ? `#${issueNumber}` : '#N';
  return `Deferred to ${number}.  This thread needs a manual resolve.  The workflow token could not resolve it (${short}).`;
}

function fenceToggle(line) {
  return /^(```|~~~)/.test(line);
}

/** A single trimmed line that is a defer command, or null. */
export function parseDeferLine(trimmed) {
  const line = String(trimmed ?? '').replace(/\r$/, '');
  if (line === '/defer') return { reason: '' };
  if (line.startsWith('/defer:')) {
    return { reason: line.slice('/defer:'.length).trim() };
  }
  if (/^\/defer[ \t]/.test(line)) {
    return { reason: line.replace(/^\/defer[ \t]+/, '').trim() };
  }
  return null;
}

/**
 * First `/defer` command line in `body`.
 * Matches a whole trimmed line that is `/defer`, `/defer <reason>`, or `/defer:<reason>`.
 * Does not match `/deferred`, a mid-line mention, or a command inside a code fence.
 * @returns {{reason: string} | null}
 */
export function parseDeferCommand(body) {
  const lines = String(body ?? '').split('\n');
  let inFence = false;
  for (const raw of lines) {
    const trimmed = raw.trim().replace(/\r$/, '');
    if (fenceToggle(trimmed)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const parsed = parseDeferLine(trimmed);
    if (parsed) return parsed;
  }
  return null;
}

/** Drop defer-command lines (outside fences) so a root comment can be the finding. */
export function stripDeferCommand(body) {
  const lines = String(body ?? '').split('\n');
  let inFence = false;
  const out = [];
  for (const raw of lines) {
    const trimmed = raw.trim().replace(/\r$/, '');
    if (fenceToggle(trimmed)) {
      inFence = !inFence;
      out.push(raw);
      continue;
    }
    if (!inFence && parseDeferLine(trimmed)) continue;
    out.push(raw);
  }
  return out.join('\n');
}

/**
 * Reject bots (type Bot, or a login ending in `[bot]`).
 * Allow collaborator permission admin, maintain, or write.
 */
export function isAllowedActor({ userType, login, permission } = {}) {
  if (userType === 'Bot') return false;
  if (typeof login === 'string' && login.endsWith('[bot]')) return false;
  const p = String(permission || '').trim().toLowerCase();
  return p === 'admin' || p === 'maintain' || p === 'write';
}

/** Severity from a shields.io `severity_level-<sev>` badge, or null. */
export function parseSeverity(body) {
  const m = String(body ?? '').match(/severity_level-(critical|high|medium|low)\b/i);
  return m ? m[1].toLowerCase() : null;
}

/**
 * Strip badge images, `<details>` blocks (and their contents), other HTML tags,
 * and HTML comments.  Collapse runs of 3+ blank lines down to two.  Trim.
 */
export function cleanFindingText(body) {
  let s = String(body ?? '');
  s = s.replace(/<details\b[^>]*>[\s\S]*?<\/details>/gi, '');
  s = s.replace(/<!--[\s\S]*?-->/g, '');
  const TAGS = 'details|summary|sub|sup|p|br|div|span|b|i|u|em|strong|a|code|pre';
  s = s.replace(new RegExp(`</?(?:${TAGS})\\b[^>]*>`, 'gi'), '');
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
  s = s.replace(/&#8203;|&#x200[Bb];|&ZeroWidthSpace;/gi, '');
  s = s.replace(/&nbsp;/gi, ' ');
  s = s.replace(/&amp;/gi, '&');
  s = s.replace(/&lt;/gi, '<');
  s = s.replace(/&gt;/gi, '>');
  s = s.replace(/&quot;/gi, '"');
  const lines = s.split('\n');
  const out = [];
  let blanks = 0;
  for (const line of lines) {
    if (line.trim() === '') {
      blanks += 1;
      if (blanks <= 2) out.push('');
    } else {
      blanks = 0;
      out.push(line);
    }
  }
  return out.join('\n').trim();
}

function stripLightMarkdown(s) {
  let t = String(s ?? '');
  t = t.replace(/`([^`]*)`/g, '$1');
  t = t.replace(/`+/g, '');
  t = t.replace(/\*\*([^*]+)\*\*/g, '$1');
  t = t.replace(/__([^_]+)__/g, '$1');
  t = t.replace(/(^|\s)\*([^*\n]+)\*(?=\s|$)/g, '$1$2');
  t = t.replace(/(^|\s)_([^_\n]+)_(?=\s|$)/g, '$1$2');
  return t.replace(/\s+/g, ' ').trim();
}

/**
 * First non-empty prose line (or its first sentence), with light markdown
 * stripped.  Longer text is cut at a word boundary and closed with an ellipsis.
 */
export function summarizeFinding(text, max = 80) {
  const lines = String(text ?? '').split('\n');
  let inFence = false;
  let prose = '';
  for (const raw of lines) {
    const trimmed = raw.trim();
    if (fenceToggle(trimmed)) {
      inFence = !inFence;
      continue;
    }
    if (inFence || !trimmed) continue;
    const withoutImages = trimmed.replace(/!\[[^\]]*\]\([^)]*\)/g, '').trim();
    if (!withoutImages) continue;
    if (/^<\/?[a-zA-Z][^>]*>$/.test(withoutImages)) continue;
    if (withoutImages.startsWith('<!--')) continue;
    prose = withoutImages;
    break;
  }
  let s = stripLightMarkdown(prose);
  const sentence = s.match(/^([\s\S]+?[.!?])(?:\s+|$)/);
  if (sentence && sentence[1].length < s.length) s = sentence[1].trim();
  if (s.length <= max) return s;
  const ellipsis = '…';
  const budget = max - ellipsis.length;
  let cut = s.lastIndexOf(' ', budget);
  if (cut < Math.floor(budget * 0.4)) cut = budget;
  return s.slice(0, cut).trimEnd() + ellipsis;
}

export function buildIssueTitle(text) {
  const summary = summarizeFinding(text, 80);
  return `Deferred review: ${summary || 'Kody finding'}`;
}

function repoFromPrUrl(prUrl) {
  const m = String(prUrl ?? '').match(/github\.com\/([^/\s]+\/[^/\s]+)\//i);
  return m ? m[1] : '';
}

/**
 * Markdown issue body.  The hidden marker is what the sweep parses:
 * `<!-- kody-defer thread=<threadId> root=<rootCommentId> path=<path> -->`
 */
export function buildIssueBody({
  finding = '',
  path = '',
  line = null,
  startLine = null,
  prUrl = '',
  prNumber = '',
  commentUrl = '',
  deferCommentUrl = '',
  reason = '',
  headSha = '',
  severity = null,
  deferredBy = '',
  threadId = '',
  rootCommentId = '',
  repo = '',
} = {}) {
  const ownerRepo = repo || repoFromPrUrl(prUrl);
  const locLine = line ?? startLine ?? '';
  const location = path ? `${path}:${locLine}` : String(locLine || '');
  const blob = ownerRepo && headSha && path && locLine !== ''
    ? `https://github.com/${ownerRepo}/blob/${headSha}/${encodePath(path)}#L${locLine}`
    : '';
  const reasonText = String(reason ?? '').trim() ? String(reason).trim() : '(none given)';
  const findingText = String(finding ?? '').trim() ? String(finding).trim() : '(no finding text)';
  const sev = severity && SEVERITIES.includes(String(severity).toLowerCase())
    ? String(severity).toLowerCase()
    : (severity || 'unknown');
  const marker = `<!-- kody-defer thread=${threadId} root=${rootCommentId} path=${path} -->`;

  const parts = [
    '## Finding',
    '',
    findingText,
    '',
    '## Location',
    '',
    blob ? `[\`${location}\`](${blob})` : `\`${location}\``,
    '',
    '## File',
    '',
    `**File:** \`${path}\``,
    '',
    '## Pull request',
    '',
    prUrl ? `[#${prNumber}](${prUrl})` : `#${prNumber}`,
    '',
    '## Thread',
    '',
    commentUrl ? `[Comment](${commentUrl})` : '(no permalink)',
  ];
  if (deferCommentUrl && deferCommentUrl !== commentUrl) {
    parts.push('', `Defer comment: ${deferCommentUrl}`);
  }
  parts.push(
    '',
    '## Deferred by',
    '',
    deferredBy ? `@${deferredBy}` : '(unknown)',
    '',
    '## Reason',
    '',
    reasonText,
    '',
    '## PR head SHA',
    '',
    `\`${headSha}\``,
    '',
    '## Severity',
    '',
    String(sev),
    '',
    marker,
    '',
  );
  return parts.join('\n');
}

function encodePath(path) {
  return String(path).split('/').map(encodeURIComponent).join('/');
}

/**
 * Issue number from a comment whose body has a line `Deferred to #<n>`, or null.
 * @param {Array<string|{body?: string}>} comments
 */
export function hasDeferredReply(comments) {
  for (const comment of comments || []) {
    const body = typeof comment === 'string' ? comment : comment?.body;
    if (!body) continue;
    if (!String(body).includes(DEFERRED_REPLY_MARKER)) continue;
    const m = String(body).match(/^Deferred to #(\d+)/m);
    if (m) return Number(m[1]);
  }
  return null;
}

/**
 * `blockedCsv` defaults to `critical`.  Case-insensitive, whitespace tolerant.
 * A null/empty severity is never blocked.
 */
export function isBlockedSeverity(severity, blockedCsv = 'critical') {
  if (severity == null || String(severity).trim() === '') return false;
  const blocked = String(blockedCsv ?? '')
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  return blocked.includes(String(severity).trim().toLowerCase());
}
