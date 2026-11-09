import { test, expect } from '@playwright/test';
import { serveRepo, serveErrors } from './serve-repo';

/**
 * Automated visual verification — never manual screenshots.
 *
 * The live digest page regenerates 4x daily, so its lede timestamp and every
 * .day card are masked.  The checked-in digest-2day fixture pins one
 * representative newest-day section plus one collapsed older-day <details>
 * and is snapshotted UNMASKED, so card markup regressions are visible.
 */
const viewports = [{ width: 1440, height: 1000 }, { width: 390, height: 844 }];

for (const viewport of viewports) {
  test(`digest header and repo legend ${viewport.width} (dynamic regions masked)`, async ({ page }) => {
    // The digest page is ~1.3 MB of generated HTML; give the first render room.
    test.setTimeout(120_000);
    await page.setViewportSize(viewport);
    await serveRepo(page);
    await page.goto('/site/index.html');
    await expect(page.locator('.legend')).toBeVisible();
    // The lede carries the digest "Created" timestamp and every .day card holds
    // date-bucketed activity that regenerates 4x daily via cron — mask them all.
    await expect(page).toHaveScreenshot(`digest-header-${viewport.width}.png`, {
      mask: [page.locator('.lede'), page.locator('.day')],
      // Baselines are generated on a Noto-Sans Linux host and CI installs
      // fonts-noto-core to match; the small allowance covers residual
      // subpixel/AA differences between hosts.
      maxDiffPixels: 200,
    });
    expect(serveErrors(page)).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test(`digest day cards render unmasked from fixture ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await serveRepo(page);
    await page.goto('/visual-tests/tests/fixtures/digest-2day.html');
    await expect(page.locator('.day').first()).toBeVisible();
    // The fixture is byte-pinned: newest day expanded, older day collapsed.
    await expect(page.locator('section.day')).toHaveCount(1);
    await expect(page.locator('details.day')).toHaveCount(1);
    await expect(page).toHaveScreenshot(`digest-fixture-${viewport.width}.png`, {
      // No masks: the whole point of the fixture is deterministic pixels.
      // Full page so both the expanded card and the collapsed <details> pin.
      fullPage: true,
      maxDiffPixels: 200,
    });
    expect(serveErrors(page)).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test(`operator start redirect fallback page ${viewport.width}`, async ({ page }) => {
    // Strip the instant meta refresh so the fallback page itself can be
    // screenshotted deterministically (the smoke spec covers the tag).
    await page.setViewportSize(viewport);
    await serveRepo(page, { stripMetaRefresh: true });
    await page.goto('/scripts/safari-start/public/index.html');
    await expect(page).toHaveScreenshot(`start-redirect-${viewport.width}.png`, { maxDiffPixels: 200 });
    expect(serveErrors(page)).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
