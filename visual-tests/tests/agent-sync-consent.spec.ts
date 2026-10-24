import { test, expect } from '@playwright/test';
import { consentPage } from '../../scripts/agent-sync-mcp/src/pages.js';

const stamp = Date.parse('2026-10-15T09:00:00Z');
const base = {
  clientName: 'Example Desktop',
  clientId: 'https://example.test/mcp.json',
  redirectHost: 'example.test',
  scopes: ['zulip:read', 'zulip:write'],
  seat: 'CURSOR',
  handle: 'h'.repeat(48),
  replacing: [{ client: 'Old Client', createdAt: stamp - 86400000 }],
};

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  // kody 4363358439: the hand-registered branch ("Registered by hand in ST-MCP
  // Admin. Its name is not verified.") needs pixel coverage.
  test(`ST-MCP consent hand-registered client ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ ...base, clientDomain: null }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    await expect(page.getByRole('heading', { name: 'Connect Example Desktop as CURSOR?', exact: true })).toBeVisible();
    await expect(page.getByText('Registered by hand in ST-MCP Admin.')).toBeVisible();
    await expect(page.locator('body')).toHaveScreenshot(`agent-sync-consent-manual-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test(`ST-MCP consent published client ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ ...base, clientDomain: 'example.test' }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    await expect(page.getByText('Published by', { exact: false })).toBeVisible();
    await expect(page.locator('body')).toHaveScreenshot(`agent-sync-consent-published-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
