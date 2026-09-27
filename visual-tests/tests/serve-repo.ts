import { readFile } from 'node:fs/promises';
import path from 'node:path';
import type { Page } from '@playwright/test';

/**
 * Serve the repo checkout to the browser without a network server.
 *
 * https://visual-tests.invalid never resolves in DNS; every request the page
 * makes is intercepted here and fulfilled straight from the repo working
 * copy.  External navigations (e.g. the start page's meta refresh to the
 * protected home) are answered 204 so they settle without leaving the page;
 * external subresources are aborted.  The run never touches the network.  This keeps visual tests
 * deterministic and immune to Chromium's local-network access checks on
 * loopback servers.
 */
export const TEST_ORIGIN = 'https://visual-tests.invalid';

// Repo root = two levels above tests/ (visual-tests/ lives at the repo root).
const REPO_ROOT = path.resolve(__dirname, '..', '..');

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

export async function serveRepo(
  page: Page,
  opts: { stripMetaRefresh?: boolean } = {},
): Promise<void> {
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== TEST_ORIGIN) {
      // External navigation (e.g. the start page's meta refresh to the
      // protected home): answer 204 so the navigation settles and the page
      // under test stays put.  External subresources are aborted outright.
      if (route.request().isNavigationRequest()) {
        await route.fulfill({ status: 204, body: '' });
      } else {
        await route.abort();
      }
      return;
    }
    const file = path.normalize(
      path.join(REPO_ROOT, decodeURIComponent(url.pathname)),
    );
    if (!file.startsWith(REPO_ROOT + path.sep)) {
      await route.abort();
      return;
    }
    try {
      let body: string | Buffer = await readFile(file);
      if (opts.stripMetaRefresh && file.endsWith('.html')) {
        // The start page redirects via <meta http-equiv="refresh" content="0;…">,
        // which would navigate away mid-screenshot.  Stripping it here keeps
        // the visual shot deterministic; the smoke spec still asserts the tag
        // exists on the unmodified page.
        body = body
          .toString('utf-8')
          .replace(/<meta[^>]*http-equiv=["']refresh["'][^>]*>/i, '');
      }
      await route.fulfill({
        body,
        contentType: MIME[path.extname(file).toLowerCase()] ?? 'application/octet-stream',
      });
    } catch {
      await route.abort();
    }
  });
}
