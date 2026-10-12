import { test, expect } from '@playwright/test';
import { serveRepo, TEST_ORIGIN } from './serve-repo';

const MOCK_GROUPS = {
  generatedAt: '2026-10-12T12:00:00.000Z',
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
            resetAt: '2026-10-14T01:30:00.000Z',
            reportedAt: '2026-10-12T11:50:00.000Z',
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
            resetAt: '2026-10-19T12:00:00.000Z',
            reportedAt: '2026-10-12T11:50:00.000Z',
            freshness: 'fresh',
          },
          sources: [],
        },
      ],
    },
  ],
};

test.describe('operator dashboard quota bars', () => {
  test('renders dual-window quotaGroups with Resets In copy', async ({ page }) => {
    await serveRepo(page);
    await page.route(`${TEST_ORIGIN}/api/quota`, async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(MOCK_GROUPS),
      });
    });
    await page.goto('/public/index.html');
    await expect(page.locator('#quotas-card .quota-window-row')).toHaveCount(2);
    await expect(page.locator('.quota-window-pair')).toBeVisible();
    await expect(page.locator('.quota-window-label').filter({ hasText: '5-hour' })).toBeVisible();
    await expect(page.locator('.quota-window-label').filter({ hasText: '7-day' })).toBeVisible();
    await expect(page.getByText('42% Remaining')).toBeVisible();
    await expect(page.locator('.quota-window-reset').first()).toContainText('Resets In');
    await expect(page.getByText('in resetting')).toHaveCount(0);
    await expect(page.getByText(/model/i)).toHaveCount(0);
    const resetColor = await page.locator('.quota-window-reset').first().evaluate((el) => getComputedStyle(el).color);
    expect(resetColor).not.toBe('rgb(161, 161, 166)');
    await page.locator('#quotas-card').screenshot({
      path: '/opt/cursor/artifacts/quota-groups-panel.png',
    });
  });
});
