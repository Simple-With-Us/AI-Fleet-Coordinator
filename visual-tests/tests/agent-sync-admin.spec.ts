import { test, expect } from '@playwright/test';
import { adminPage } from '../../scripts/agent-sync-mcp/src/pages.js';

const stamp = Date.parse('2026-10-15T09:00:00Z');
const call = { ts: stamp - 30000, seat: 'CURSOR', tool: 'linear_query', outcome: 'error', error_code: 'budget_exhausted', error_message: 'The hourly Linear read budget is exhausted.', retryable: true, reset_at: stamp + 1800000, grant_ref: '0123456789ab', client_id: 'https://cursor.com/oauth/mcp-client.json' };
const facts = {
  email: 'admin@example.test', generatedAt: stamp, csrf: 'c'.repeat(64), notice: '',
  seats: [
    { seat: 'CURSOR', epoch: 2, paused: false, armed: false, keyInstalled: true, role: { ok: true, role: 400, checked_at: stamp - 60000 }, grants: [{ client: 'Cursor', scope: ['zulip:read', 'zulip:write'], createdAt: stamp - 86400000 }], linearEnabled: true, linearConfigured: true, linearWrites: true, recallConfigured: true },
    { seat: 'JET', epoch: 1, paused: false, armed: false, keyInstalled: true, role: null, grants: [], linearEnabled: false, linearConfigured: false, linearWrites: false, recallConfigured: true },
  ],
  calls: [call], statusCalls: [call], statusErrors: [call],
  clients: [], audits: [], statusAudits: [], tokenRefusals: [], runtimeErrors: [], manualSeats: [],
  refusals: [{ ts: stamp - 120000, reason: 'cimd_client_not_allowlisted', client_id: 'https://example.test/mcp.json', redirect_uri: 'https://example.test/callback' }],
};

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`ST-MCP Admin Status ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = adminPage(facts);
    const html = await response.text();
    await page.route('https://visual-tests.invalid/admin', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/admin');
    await expect(page.getByRole('heading', { name: 'ST-MCP Admin', exact: true })).toBeVisible();
    // The status overview folds by default (AFC-347); open it before asserting on its cards.
    await page.locator('details#status-overview > summary').click();
    await expect(page.locator('.error-card').filter({ hasText: 'The hourly Linear read budget is exhausted.' }).first()).toBeVisible();
    // Empty manual-seat state: the section is deliberately absent (kody 4364333458 - both
    // states need coverage; this full-page screenshot is the empty baseline).
    await expect(page.getByRole('heading', { name: 'Manual seats' })).toHaveCount(0);
    await expect(page.locator('body')).toHaveScreenshot(`agent-sync-status-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test(`ST-MCP Admin manual seats populated ${viewport.width}`, async ({ page }) => {
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
