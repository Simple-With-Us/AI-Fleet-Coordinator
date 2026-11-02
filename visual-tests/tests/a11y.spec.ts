import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { serveRepo, serveErrors } from './serve-repo';

/**
 * Accessibility pin: zero serious-or-worse violations on both pages, at both
 * viewport widths (reuses the agent-sync specs' viewport loop).
 */
const viewports = [{ width: 1440, height: 1000 }, { width: 390, height: 844 }];

for (const viewport of viewports) {
  test(`digest page has no serious a11y violations ${viewport.width}`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize(viewport);
    await serveRepo(page);
    await page.goto('/site/index.html');
    await expect(page.locator('.day').first()).toBeVisible();
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    const blocking = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(blocking).toEqual([]);
    expect(serveErrors(page)).toEqual([]);
  });

  test(`operator start page has no serious a11y violations ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await serveRepo(page, { stripMetaRefresh: true });
    await page.goto('/scripts/safari-start/public/index.html');
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    const blocking = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(blocking).toEqual([]);
    expect(serveErrors(page)).toEqual([]);
  });
}
