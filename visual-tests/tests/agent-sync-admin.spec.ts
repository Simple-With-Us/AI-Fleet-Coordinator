import { test, expect } from '@playwright/test';
import { adminPage } from '../../scripts/agent-sync-mcp/src/pages.js';

const stamp = Date.parse('2026-10-15T09:00:00Z');
const call = { ts: stamp - 30000, seat: 'CURSOR', tool: 'linear_query', outcome: 'error', error_code: 'budget_exhausted', error_message: 'The hourly Linear read budget is exhausted.', retryable: true, reset_at: stamp + 1800000, grant_ref: '0123456789ab', client_id: 'https://cursor.com/oauth/mcp-client.json' };
const facts = {
  email: 'admin@example.test', generatedAt: stamp, csrf: 'c'.repeat(64), notice: '',
  seats: [
    { seat: 'CURSOR', epoch: 2, paused: false, armed: false, keyInstalled: true, role: { ok: true, role: 400, checked_at: stamp - 60000 }, grants: [{ client: 'Cursor', scope: ['zulip:read', 'zulip:write'], createdAt: stamp - 86400000 }], linearEnabled: true, linearConfigured: true, linearWrites: true, recallConfigured: true },
    { seat: 'JET', epoch: 1, paused: false, armed: false, keyInstalled: true, role: null, grants: [], linearEnabled: false, linearConfigured: false, linearWrites: false, recallConfigured: true },
    { seat: 'JET-CHAT', epoch: 1, paused: false, armed: false, keyInstalled: true, role: { ok: true, role: 400, checked_at: stamp - 60000 }, grants: [{ client: 'ChatGPT', scope: ['zulip:read', 'zulip:write'], createdAt: stamp - 43200000 }], linearEnabled: true, linearConfigured: true, linearWrites: false, recallConfigured: true },
  ],
  calls: [call], statusCalls: [call], statusErrors: [call],
  clients: [], audits: [], statusAudits: [], tokenRefusals: [], runtimeErrors: [], manualSeats: [],
  refusals: [{ ts: stamp - 120000, reason: 'cimd_client_not_allowlisted', client_id: 'https://example.test/mcp.json', redirect_uri: 'https://example.test/callback' }],
};

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`Fleet MCP Admin Status ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = adminPage(facts);
    const html = await response.text();
    await page.route('https://visual-tests.invalid/admin', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/admin');
    // The page answers to BOTH servers now (AFC-347):  the renamed title is
    // asserted as the document title, the visible h1, and its own screenshot
    // baseline so a rename regression is caught by name and by pixels
    // (kody 4372747419).
    await expect(page).toHaveTitle('Fleet MCP Admin');
    const heading = page.getByRole('heading', { name: 'Fleet MCP Admin', exact: true });
    await expect(heading).toBeVisible();
    await expect(heading).toHaveScreenshot(`agent-sync-admin-title-${viewport.width}.png`, { animations: 'disabled', maxDiffPixels: 0 });
    // The status overview folds by default (AFC-347); open it before asserting on its cards.
    await page.locator('details#status-overview > summary').click();
    await expect(page.locator('.error-card').filter({ hasText: 'The hourly Linear read budget is exhausted.' }).first()).toBeVisible();
    // Empty manual-seat state: the section is deliberately absent (kody 4364333458 - both
    // states need coverage; this full-page screenshot is the empty baseline).
    await expect(page.getByRole('heading', { name: 'Manual seats' })).toHaveCount(0);
    await expect(page.locator('body')).toHaveScreenshot(`agent-sync-status-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  // The runtime-secrets line under the Seats table (PR #901, kody 4366922942), in its warning
  // state:  fallback mode with fallbacks above zero.  The element alone, so the full-page
  // baselines above stay as they are.  Nothing in it is live:  the mode and every counter
  // come from the fixture below and the line has no time, so nothing is masked (a mask
  // over p.warn would blank the element under test), and the tolerance is an explicit
  // zero pixels (kody 4367022364, 4367022518).
  test(`Fleet MCP Admin Secrets Mode ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = adminPage({ ...facts, infisical: { mode: 'fallback', fetched: 5, cacheHits: 40, fallbacks: 2, absent: 5 } });
    const html = await response.text();
    await page.route('https://visual-tests.invalid/admin', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/admin');
    const line = page.locator('p.warn', { hasText: 'Secrets mode:' });
    await expect(line).toBeVisible();
    await expect(line).toContainText('2 fallbacks to Worker bindings');
    await expect(line).toHaveScreenshot(`agent-sync-secrets-mode-${viewport.width}.png`, { animations: 'disabled', maxDiffPixels: 0 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  // The per-identity bearer mismatch counters (the wrong-secret storm
  // signal):  present (small and seven-figure), read-unavailable, and inert
  // zero in one table.  ownerTime is fixed-zone, so a fixed stamp renders
  // deterministically and no mask is needed.
  test(`Fleet MCP Admin bearer mismatch counters ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = adminPage({ ...facts, gbBearers: [
      { identity: 'JET', kind: 'seat', credentials: true,
        status: { active: true, expired: false, created_at: stamp - 86400000, expires_at: stamp + 86400000, last_used_at: stamp - 60000, mcp_accept: true },
        mismatches: { count: 3, last_at: stamp - 120000 } },
      { identity: 'CLAUDE', kind: 'seat', credentials: true,
        status: { active: true, expired: false, created_at: stamp - 86400000, expires_at: stamp + 86400000, mcp_accept: false },
        mismatches: { count: 1234567, last_at: stamp - 30000 } },
      { identity: 'CURSOR', kind: 'seat', credentials: true, status: null, mismatchesUnavailable: true },
      { identity: 'GB-FIXER', kind: 'persona', credentials: true, status: { active: false, expired: false }, mismatches: null },
    ] });
    const html = await response.text();
    await page.route('https://visual-tests.invalid/admin', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/admin');
    await expect(page.getByText('3 wrong-secret attempts', { exact: false })).toBeVisible();
    await expect(page.getByText('1234567 wrong-secret attempts', { exact: false })).toBeVisible();
    await expect(page.getByText('wrong-secret counter read unavailable')).toBeVisible();
    const section = page.locator('table', { has: page.getByText('1234567 wrong-secret attempts') }).first();
    await expect(section).toHaveScreenshot(`agent-sync-bearer-mismatches-${viewport.width}.png`, { animations: 'disabled', maxDiffPixels: 0 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test(`Fleet MCP Admin manual seats populated ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    // All three redirect-status variants: configured, configured+shared, none yet.
    const populated = { ...facts, manualSeats: [
      { seat: 'CURSOR', redirectsConfigured: true, shared: false },
      { seat: 'GROK_WEB', redirectsConfigured: true, shared: true },
      { seat: 'JET', redirectsConfigured: false, shared: false },
    ] };
    const response = adminPage(populated);
    const html = await response.text();
    await page.route('https://visual-tests.invalid/admin', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/admin');
    await expect(page.getByRole('heading', { name: 'Manual seats' })).toBeVisible();
    await expect(page.getByRole('cell', { name: 'Configured, shared with another seat of the same app' })).toBeVisible();
    await expect(page.locator('body')).toHaveScreenshot(`agent-sync-manual-seats-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
