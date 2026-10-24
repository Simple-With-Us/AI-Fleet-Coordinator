import { readFile } from 'node:fs/promises';
import path from 'node:path';
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
  test('source pins the zero-delay redirect to the protected home', async () => {
    // Source-level pin:  the production page redirects IMMEDIATELY (delay 0) to
    // the protected home.  The DOM test below serves a delay-rewritten copy, so
    // it cannot catch a production delay/target regression - this one fails on
    // any change to either.
    const html = await readFile(
      path.resolve(__dirname, '..', '..', 'scripts/safari-start/public/index.html'),
      'utf-8',
    );
    expect(html).toContain(
      '<meta http-equiv="refresh" content="0;url=https://home.jays.services/">',
    );
  });

  test('links to the protected home and carries the refresh redirect', async ({ page }) => {
    await serveRepo(page, { deferMetaRefresh: true });
    await page.goto('/scripts/safari-start/public/index.html');
    const link = page.getByRole('link', { name: 'Continue to home' });
    await expect(link).toHaveAttribute('href', 'https://home.jays.services/');
    const refresh = await page
      .locator('meta[http-equiv="refresh"]')
      .getAttribute('content');
    expect(refresh).toContain('https://home.jays.services/');
  });
});
