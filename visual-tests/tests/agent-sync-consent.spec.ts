import { test, expect } from '@playwright/test';
import { consentPage } from '../../scripts/agent-sync-mcp/src/pages.js';

const stamp = Date.parse('2026-10-15T09:00:00Z');
const base = {
  clientName: 'Example Desktop',
  clientId: 'https://example.test/mcp.json',
  redirectHost: 'example.test',
  scopes: ['zulip:read', 'zulip:write'],
  seat: 'CURSOR',
  handle: 'h'.repeat(48),
  replacing: [{ client: 'Old Client', createdAt: stamp - 86400000 }],
  // consentPage requires eventsOnly from the loaded config; CURSOR is a full seat.
  eventsOnly: false,
};

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  // kody 4363358439: the hand-registered branch ("Registered by hand in Fleet MCP
  // Admin. Its name is not verified.") needs pixel coverage.
  test(`Fleet MCP consent hand-registered client ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ ...base, clientDomain: null }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    // The consent heading names the seat by its admin display label (adminSeatLabel).
    await expect(page.getByRole('heading', { name: 'Connect Example Desktop as Cursor & Grok Bots?', exact: true })).toBeVisible();
    await expect(page.getByText('Registered by hand in Fleet MCP Admin.')).toBeVisible();
    await expect(page.locator('body')).toHaveScreenshot(`agent-sync-consent-manual-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test(`Fleet MCP consent published client ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ ...base, clientDomain: 'example.test' }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    await expect(page.getByText('Published by', { exact: false })).toBeVisible();
    await expect(page.locator('body')).toHaveScreenshot(`agent-sync-consent-published-${viewport.width}.png`, { animations: 'disabled', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  // Jet-Wake is the wake-only connection:  its consent offer is zulip:read only
  // (the POST handler also enforces it - covered behaviorally in
  // integration.workerd.mjs; this pins the rendered one-scope page).
  test(`Jet-Wake wake-only consent ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ clientName: 'ChatGPT', clientDomain: 'chatgpt.com', clientId: 'https://chatgpt.com/connector/mcp.json', redirectHost: 'chatgpt.com', seat: 'JET', scopes: ['zulip:read'], handle: 'h'.repeat(48), replacing: [], eventsOnly: true }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    await expect(page.getByRole('heading', { name: 'Connect ChatGPT as Jet-Wake?', exact: true })).toBeVisible();
    await expect(page.locator('input[name="scope"]')).toHaveCount(1);
    await expect(page.locator('body')).toHaveScreenshot(`jet-wake-consent-${viewport.width}.png`, { animations: 'disabled', fullPage: true, maxDiffPixels: 100, mask: [page.locator('time'), page.locator('.timestamp'), page.locator('[data-dynamic]')] });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  // The full Jet login with the WhatsApp bridge on:  the consent must disclose
  // private WhatsApp inbox read/ack, named apart from Zulip privacy (kody
  // 4365431558).  whatsapp:true matches the live gate (seat JET-CHAT +
  // config.whatsappEnabled).
  test(`Jet full consent with WhatsApp disclosure ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ clientName: 'ChatGPT', clientDomain: 'chatgpt.com', clientId: 'https://chatgpt.com/connector/mcp.json', redirectHost: 'chatgpt.com', seat: 'JET-CHAT', scopes: ['zulip:read', 'zulip:write'], handle: 'h'.repeat(48), replacing: [], whatsapp: true, eventsOnly: false }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    await expect(page.getByRole('heading', { name: 'Connect ChatGPT as Jet?', exact: true })).toBeVisible();
    await expect(page.getByText("It can also read and acknowledge messages in the owner's private WhatsApp inbox.")).toBeVisible();
    await expect(page.getByText('Private Zulip channels are never open to it.')).toBeVisible();
    await expect(page.locator('body')).toHaveScreenshot(`jet-chat-whatsapp-consent-${viewport.width}.png`, { animations: 'disabled', fullPage: true, maxDiffPixels: 100, mask: [page.locator('time'), page.locator('.timestamp'), page.locator('[data-dynamic]')] });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  // Regression: with the bridge off the same full seat never mentions WhatsApp.
  test(`Jet full consent without WhatsApp ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const response = consentPage({ clientName: 'ChatGPT', clientDomain: 'chatgpt.com', clientId: 'https://chatgpt.com/connector/mcp.json', redirectHost: 'chatgpt.com', seat: 'JET-CHAT', scopes: ['zulip:read', 'zulip:write'], handle: 'h'.repeat(48), replacing: [], eventsOnly: false }, {});
    const html = await response.text();
    await page.route('https://visual-tests.invalid/authorize', route => route.fulfill({ status: 200, contentType: 'text/html', body: html, headers: Object.fromEntries(response.headers) }));
    await page.goto('/authorize');
    await expect(page.getByRole('heading', { name: 'Connect ChatGPT as Jet?', exact: true })).toBeVisible();
    await expect(page.getByText('Private Zulip channels are never open to it.')).toBeVisible();
    await expect(page.getByText(/WhatsApp/)).toHaveCount(0);
  });
}
