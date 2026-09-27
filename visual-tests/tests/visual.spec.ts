import { test, expect } from '@playwright/test';
import { serveRepo } from './serve-repo';

/**
 * Automated visual verification — never manual screenshots.
 *
 * Both pages are static; the only time-varying parts are masked rather than
 * skipped so the snapshots stay deterministic across digest regenerations.
 */
test('digest header and repo legend (dynamic regions masked)', async ({ page }) => {
  // The digest page is ~1.3 MB of generated HTML; give the first render room.
  test.setTimeout(120_000);
  await serveRepo(page);
  await page.goto('/site/index.html');
  await expect(page.locator('.legend')).toBeVisible();
  // The lede carries the digest "Created" timestamp and every .day card holds
  // date-bucketed activity that regenerates 4x daily via cron — mask them all.
  await expect(page).toHaveScreenshot('digest-header.png', {
    mask: [page.locator('.lede'), page.locator('.day')],
    // Baselines are generated on a Noto-Sans Linux host and CI installs
    // fonts-noto-core to match; the small allowance covers residual
    // subpixel/AA differences between hosts.
    maxDiffPixels: 200,
  });
});

test('operator start redirect fallback page', async ({ page }) => {
  // Strip the instant meta refresh so the fallback page itself can be
  // screenshotted deterministically (the smoke spec covers the tag).
  await serveRepo(page, { stripMetaRefresh: true });
  await page.goto('/scripts/safari-start/public/index.html');
  await expect(page).toHaveScreenshot('start-redirect.png');
});
