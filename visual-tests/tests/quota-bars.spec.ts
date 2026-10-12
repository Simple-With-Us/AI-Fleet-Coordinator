import { test, expect } from '@playwright/test';
import { serveRepo, TEST_ORIGIN } from './serve-repo';

function mockQuotaGroups() {
  const now = Date.now();
  const reportedAt = new Date(now - 5 * 60_000).toISOString();
  const reset5h = new Date(now + 2.5 * 3600_000).toISOString();
  const reset7d = new Date(now + 4 * 86400_000).toISOString();
  return {
    generatedAt: new Date(now).toISOString(),
    windows: [],
    quotaGroups: [
      {
        provider: 'claude',
        providerLabel: 'Claude',
        windows: [
          {
            windowKey: '5h',
            displayName: '5 Hour',
            rawKey: 'five_hour',
            primary: {
              source: 'codecaps',
              sourceLabel: 'CodeCaps',
              remainingPercent: 42,
              remainingUnknown: false,
              isExhausted: false,
              resetAt: reset5h,
              reportedAt,
              freshness: 'fresh',
            },
            sources: [],
          },
          {
            windowKey: '7d',
            displayName: 'Weekly',
            rawKey: 'seven_day',
            primary: {
              source: 'codecaps',
              remainingPercent: 80,
              remainingUnknown: false,
              isExhausted: false,
              resetAt: reset7d,
              reportedAt,
              freshness: 'fresh',
            },
            sources: [],
          },
        ],
      },
    ],
  };
}

test.describe('operator dashboard quota bars', () => {
  test('renders dual-window quotaGroups with Resets In copy', async ({ page }) => {
    await serveRepo(page);
    await page.route(`${TEST_ORIGIN}/api/quota`, async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(mockQuotaGroups()),
      });
    });
    await page.goto('/public/index.html');
    await expect(page.locator('#quotas-card .quota-cc-row')).toHaveCount(1);
    await expect(page.locator('.quota-cc-win')).toHaveCount(2);
    await expect(page.locator('.quota-cc-win-label').filter({ hasText: '5-hour' })).toBeVisible();
    await expect(page.locator('.quota-cc-win-label').filter({ hasText: '7-day' })).toBeVisible();
    await expect(page.locator('.qcap-pill').filter({ hasText: '42%' })).toBeVisible();
    await expect(page.locator('.qcap-pill-ok').first()).toBeVisible();
    await expect(page.getByText(/Remaining/i)).toHaveCount(0);
    await expect(page.locator('.qcap-elapsed')).toHaveCount(2);
    await expect(page.locator('.quota-cc-win-reset').first()).not.toContainText('Resets In');
    await expect(page.getByText('in resetting')).toHaveCount(0);
    await expect(page.getByText(/model/i)).toHaveCount(0);
    const resetColor = await page.locator('.quota-cc-win-reset').first().evaluate((el) => getComputedStyle(el).color);
    expect(resetColor).not.toBe('rgb(161, 161, 166)');
    await expect(page.locator('#quotas-card')).toHaveScreenshot('quota-groups-panel.png', {
      maxDiffPixelRatio: 0.02,
      // Both of these render live countdown / "x ago" copy, so mask them or the
      // baseline drifts every minute.
      mask: [page.locator('.quota-cc-win-reset'), page.locator('#quota-meta')],
    });
  });
});
