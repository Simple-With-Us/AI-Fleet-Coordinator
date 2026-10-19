import { test, expect } from '@playwright/test';
import { serveRepo } from './serve-repo';

test.describe('digest page (site/index.html)', () => {
  test('loads with header, repo legend, and day cards', async ({ page }) => {
    await serveRepo(page);
    await page.goto('/site/index.html');
    await expect(page.locator('h1')).toHaveText("Jay's Daily Log");
    await expect(page.locator('.legend')).toBeVisible();
    await expect(page.locator('.day').first()).toBeVisible();
  });

  test('repo legend lists the fleet surfaces', async ({ page }) => {
    await serveRepo(page);
    await page.goto('/site/index.html');
    for (const label of ['Socratic.Trade', 'Congress.Trade', 'BotFleet.app', 'Hog Hunter']) {
      await expect(page.locator('.legend')).toContainText(label);
    }
  });
});

test.describe('operator start redirect page (scripts/safari-start/public/index.html)', () => {
  test('links to the protected home and carries the refresh redirect', async ({ page }) => {
    await serveRepo(page);
    await page.goto('/scripts/safari-start/public/index.html');
    const link = page.getByRole('link', { name: 'Continue to home' });
    await expect(link).toHaveAttribute('href', 'https://home.jays.services/');
    const refresh = await page
      .locator('meta[http-equiv="refresh"]')
      .getAttribute('content');
    expect(refresh).toContain('https://home.jays.services/');
  });
});
