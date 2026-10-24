import { test, expect } from '@playwright/test';
import { consentPage } from '../../scripts/agent-sync-mcp/src/pages.js';

const stamp = Date.parse('2026-10-15T09:00:00Z');

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  // Jet-Wake is the wake-only connection:  its consent offer is zulip:read only
  // (the POST handler also enforces it - covered behaviorally in
  // integration.workerd.mjs; this pins the rendered one-scope page).
  test(`Jet-Wake wake-only consent ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ clientName: 'ChatGPT', clientDomain: 'chatgpt.com', clientId: 'https://chatgpt.com/connector/mcp.json', redirectHost: 'chatgpt.com', seat: 'JET', scopes: ['zulip:read'], handle: 'h'.repeat(48), replacing: [] }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    await expect(page.getByRole('heading', { name: 'Connect ChatGPT as Jet-Wake?', exact: true })).toBeVisible();
    await expect(page.locator('input[name="scope"]')).toHaveCount(1);
    await expect(page.locator('body')).toHaveScreenshot(`jet-wake-consent-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
