import { test, expect } from '@playwright/test';
import { errorPage, noConnectionPage, gbBearerRevealPage } from '../../scripts/agent-sync-mcp/src/pages.js';

// The Fleet MCP pages whose copy sends the owner back to Fleet MCP Admin (kody 4363358439,
// "also found in"):  the shared errorPage template with one of the OAuth refusals that
// names the admin (every other refusal in src/index.js renders through the same template),
// the no-connection page, and the bearer reveal page's back link.  Synthetic data only.
const expiresAt = Date.parse('2026-10-15T09:00:00Z');
const cases = [
  {
    name: 'seats-armed',
    render: () => errorPage('More Than One Seat Armed', "These seats share this app's callback:  CURSOR, GROK-WEB.  More than one is armed, so the connection cannot tell which one you meant.  Disarm all but one in Fleet MCP Admin, then start the connection again.", 409),
    heading: 'More Than One Seat Armed',
    copy: 'Disarm all but one in Fleet MCP Admin',
  },
  {
    name: 'no-connection',
    render: () => noConnectionPage(),
    heading: 'No Connection Expected',
    copy: 'open Fleet MCP Admin, arm the seat',
  },
  {
    name: 'bearer-reveal',
    render: () => gbBearerRevealPage({ identity: 'GB-FIXER', token: 'EXAMPLE-ONLY', expiresAt }),
    heading: 'Bearer For GB-FIXER',
    copy: 'Back to Fleet MCP Admin',
  },
];

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  for (const c of cases) {
    test(`Fleet MCP ${c.name} page ${viewport.width}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      const response = c.render();
      const html = await response.text();
      await page.route('https://visual-tests.invalid/page', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
      await page.goto('/page');
      await expect(page.getByRole('heading', { name: c.heading, exact: true })).toBeVisible();
      await expect(page.getByText(c.copy, { exact: false })).toBeVisible();
      await expect(page.locator('body')).toHaveScreenshot(`agent-sync-${c.name}-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    });
  }
}
