import { defineConfig, devices } from '@playwright/test';

/**
 * Automated visual verification for the fleet's web surfaces.
 *
 * Screens covered:
 *  - site/index.html        — the fleet daily digest GitHub Pages site ("Jay's Daily Log").
 *                             Content regenerates 4x daily via cron, so the lede
 *                             timestamp and every .day card are masked in snapshots.
 *  - scripts/safari-start/public/index.html — the operator start redirect fallback page.
 *
 * Pages are served to the browser via request interception (see
 * tests/serve-repo.ts) against https://visual-tests.invalid, which never
 * resolves — every request is fulfilled from the repo checkout.  This keeps
 * the run fully offline and sidesteps Chromium >= 142 local-network access
 * checks that block loopback webServers.
 */
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  // The digest page is ~1.3 MB of generated HTML and shared CI runners can
  // be slow to launch browsers; keep the per-test budget generous.
  timeout: 60_000,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: 'https://visual-tests.invalid',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // The suite is fully offline (request interception serves the repo
        // checkout); bypass any ambient HTTP(S)_PROXY so fake origins like
        // visual-tests.invalid can never hang in proxy DNS.
        launchOptions: {
          ...(process.env.PW_CHROMIUM_PATH
            ? { executablePath: process.env.PW_CHROMIUM_PATH }
            : {}),
          args: ['--no-proxy-server'],
        },
      },
    },
  ],
  snapshotPathTemplate: '{testDir}/{testFileBaseName}-snapshots/{arg}{ext}',
});
