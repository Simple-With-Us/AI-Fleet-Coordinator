#!/usr/bin/env node
// Find the service tokens that are installed on a host but have no row in the
// Infisical Service Tokens registry.
//
// The registry lives in the Fleet Reference sheet; every consumer keeps a
// metadata sidecar that names the token id it holds.  This script compares
// the two and reports the tokens that are in use without a row, so a token
// minted and never registered is found by a check rather than by an accident.
//
// It prints only nonsecret metadata: token ids, names, projects and expiries.
// It never reads a token value and never writes one anywhere.  A read that
// fails is a failure:  the check concludes nothing rather than concluding
// "no gap" from a directory it could not read.
//
//   node scripts/service_token_registry_check.mjs --sidecar-dir ~/.config/infisical \
//       --sheet-rows rows.jsonl [--inventory peer.json]...
//   node scripts/service_token_registry_check.mjs --print-inventory --sidecar-dir <dir>
//   node scripts/service_token_registry_check.mjs --fleet-ops-root <Fleet-OPS checkout> \
//       --sidecar-dir <dir> [--inventory peer.json]...
//
// Exit codes:  0 no token is missing its row, 1 a token is missing its row,
// 3 a read failed (nothing is concluded).

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const SHEET = 'Infisical Service Tokens';
// The only fields of a sidecar or a peer inventory this script keeps.  A
// handed-over inventory file is not trusted to hold only these keys.
const KEEP = ['id', 'name', 'project', 'expiresAt', 'file'];

function parseArgs(argv) {
  const args = { sidecarDir: join(homedir(), '.config', 'infisical'), inventories: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    const value = argv[i + 1];
    const needsValue = ['--sidecar-dir', '--sheet-rows', '--fleet-ops-root', '--inventory'];
    if (needsValue.includes(flag)) {
      if (value === undefined || value.startsWith('--')) {
        throw new Error(`${flag} needs a value`);
      }
      i += 1;
    }
    switch (flag) {
      case '--sidecar-dir': args.sidecarDir = value; break;
      case '--sheet-rows': args.sheetRows = value; break;
      case '--fleet-ops-root': args.fleetOpsRoot = value; break;
      case '--inventory': args.inventories.push(value); break;
      case '--print-inventory': args.printInventory = true; break;
      case '--json': args.json = true; break;
      case '--help': case '-h': args.help = true; break;
      default: throw new Error(`unknown flag ${flag}`);
    }
  }
  return args;
}

// The sidecars a consumer keeps beside its stored value.  Two shapes exist:
// the Mac's carries `folder` and `scope` beside `scopes`, the server's carries
// the scope inside `scopes` alone.  The fields this script needs (`id` and
// `name`) are the same in both.
//
// A directory that cannot be read, and a sidecar that cannot be parsed, are
// read failures:  the check then cannot say which tokens are installed, so it
// concludes nothing instead of concluding that there is no gap.
export function readSidecars(directory) {
  const found = [];
  const unreadable = [];
  let entries;
  try {
    entries = readdirSync(directory);
  } catch (error) {
    const code = error && error.code ? error.code : 'unknown';
    throw new Error(`the sidecar directory ${directory} could not be read (${code})`);
  }
  for (const entry of entries.sort()) {
    if (!entry.endsWith('.json')) continue;
    const path = join(directory, entry);
    try {
      if (!statSync(path).isFile()) continue;
      const data = JSON.parse(readFileSync(path, 'utf8'));
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        unreadable.push(entry);
        continue;
      }
      const id = typeof data.id === 'string' ? data.id.trim()
        : typeof data.tokenId === 'string' ? data.tokenId.trim() : '';
      if (!id) continue;
      found.push({
        id,
        name: typeof data.name === 'string' ? data.name : '',
        project: typeof data.project === 'string' ? data.project : (typeof data.projectId === 'string' ? data.projectId : ''),
        expiresAt: data.expiresAt === null || data.expiresAt === undefined ? null : String(data.expiresAt),
        file: entry,
      });
    } catch {
      unreadable.push(entry);
    }
  }
  return { found, unreadable };
}

// Another host's inventory, kept to the same fields as a local sidecar:  a
// file handed over from another host is not trusted to hold only these keys.
//
// The file is a dedicated inventory, so every entry in it is an installed
// token.  An entry that is not an object, or that names no token id, is a
// malformed entry and a read failure:  dropping it would let an installed
// token vanish and let the check report no gap.
export function readPeerInventory(path) {
  let data;
  try {
    data = JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    const code = error && error.code ? error.code : 'unparsable';
    throw new Error(`the peer inventory ${path} could not be read (${code})`);
  }
  if (!data || typeof data !== 'object' || !Array.isArray(data.tokens)) {
    throw new Error(`the peer inventory ${path} has no token list`);
  }
  const peers = [];
  const malformed = [];
  for (const [index, token] of data.tokens.entries()) {
    if (!token || typeof token !== 'object' || Array.isArray(token)) {
      malformed.push(`entry ${index}`);
      continue;
    }
    const id = typeof token.id === 'string' ? token.id.trim() : '';
    if (!id) {
      malformed.push(`entry ${index}`);
      continue;
    }
    peers.push({
      id,
      name: typeof token.name === 'string' ? token.name : '',
      project: typeof token.project === 'string' ? token.project : '',
      expiresAt: token.expiresAt === null || token.expiresAt === undefined ? null : String(token.expiresAt),
      file: `${path}:${typeof token.file === 'string' ? token.file : ''}`,
    });
  }
  if (malformed.length) {
    throw new Error(`the peer inventory ${path} has ${malformed.length} malformed entry: ${malformed.join(', ')}`);
  }
  return peers;
}

// The registry rows: either a JSON-lines file, or the live sheet through the
// shared Fleet-OPS client.
export async function readSheetRows(args) {
  if (args.sheetRows) {
    const rows = [];
    for (const line of readFileSync(args.sheetRows, 'utf8').split('\n')) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('{')) continue;
      const row = JSON.parse(trimmed);
      const id = typeof row['Token ID'] === 'string' ? row['Token ID'].trim() : '';
      if (!id) continue;
      rows.push({ id, name: String(row.Token ?? ''), status: String(row.Status ?? ''), expiresAt: String(row['Expires (UTC)'] ?? '') });
    }
    return rows;
  }
  if (!args.fleetOpsRoot) throw new Error('the sheet needs --sheet-rows or --fleet-ops-root');
  if (!process.env.INFISICAL_TOKEN || !process.env.INFISICAL_FLEET_OPS_PROJECT_ID) {
    throw new Error('the live sheet read needs INFISICAL_TOKEN and INFISICAL_FLEET_OPS_PROJECT_ID');
  }
  const { api } = await import(pathToFileURL(join(args.fleetOpsRoot, 'scripts/fleet-sheet/sa.mjs')).href);
  const answer = await api('/values/' + encodeURIComponent(SHEET) + '?majorDimension=ROWS');
  const [headers, ...rows] = answer.values ?? [];
  const idColumn = headers.indexOf('Token ID');
  if (idColumn < 0) throw new Error('the sheet has no "Token ID" column');
  const out = [];
  for (const row of rows) {
    if (!row) continue;
    const id = String(row[idColumn] ?? '').trim();
    if (!id) continue;
    out.push({
      id,
      name: String(row[headers.indexOf('Token')] ?? ''),
      status: String(row[headers.indexOf('Status')] ?? ''),
      expiresAt: String(row[headers.indexOf('Expires (UTC)')] ?? ''),
    });
  }
  return out;
}

export function findGaps(installed, rows) {
  const registered = new Set(rows.map(row => (typeof row.id === 'string' ? row.id.trim() : row.id)));
  const missing = installed.filter(token => !registered.has(typeof token.id === 'string' ? token.id.trim() : token.id));
  return { missing };
}

// Two sidecar files on one host that name the same token id are a hygiene
// signal, not a registry gap.  The same token id on two hosts is not a
// duplicate at all:  a consumer that runs on both hosts keeps a sidecar on
// each, so the hosts are counted separately.
export function findDuplicateSidecars(installed) {
  const seen = new Set();
  const duplicates = [];
  for (const token of installed) {
    if (seen.has(token.id)) {
      duplicates.push(token);
      continue;
    }
    seen.add(token.id);
  }
  return duplicates;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write('usage: service_token_registry_check.mjs [--sidecar-dir dir] [--sheet-rows file | --fleet-ops-root dir] [--inventory peer.json] [--print-inventory] [--json]\n');
    return 0;
  }

  const { found: installed, unreadable } = readSidecars(args.sidecarDir);
  if (unreadable.length) {
    throw new Error(`the sidecar(s) ${unreadable.join(', ')} could not be read`);
  }
  if (args.printInventory) {
    process.stdout.write(JSON.stringify({ host: homedir(), tokens: installed.map(token => {
      const kept = {};
      for (const key of KEEP) kept[key] = token[key];
      return kept;
    }) }, null, 2) + '\n');
    return 0;
  }

  const peers = [];
  for (const path of args.inventories) peers.push(...readPeerInventory(path));

  const rows = await readSheetRows(args);
  const all = [...installed, ...peers];
  const { missing } = findGaps(all, rows);
  const duplicates = findDuplicateSidecars(installed);

  if (args.json) {
    process.stdout.write(JSON.stringify({
      installed: all.length,
      registryRows: rows.length,
      missing,
      duplicates,
    }, null, 2) + '\n');
  } else {
    process.stdout.write(`installed service tokens: ${all.length} (${installed.length} on this host, ${peers.length} from peer inventories)\n`);
    process.stdout.write(`registry rows: ${rows.length}\n`);
    for (const token of missing) {
      process.stdout.write(`MISSING ROW: ${token.id} ${token.name} (${token.file}) expires=${token.expiresAt ?? 'null'}\n`);
    }
    for (const token of duplicates) {
      process.stdout.write(`DUPLICATE SIDECAR: ${token.id} ${token.name} (${token.file}) names a token another sidecar on this host already names\n`);
    }
    if (!missing.length) process.stdout.write('every installed service token has a registry row\n');
  }
  return missing.length ? 1 : 0;
}

try {
  process.exitCode = await main();
} catch (error) {
  process.stderr.write(`service-token registry check: ${error && error.message ? error.message : 'the read failed'}\n`);
  process.exitCode = 3;
}
