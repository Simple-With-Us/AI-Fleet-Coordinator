import test from 'node:test';
import assert from 'node:assert/strict';
import { parse } from 'graphql';
import { HostedTools, HOSTED_TOOL_LIST } from '../src/hosted-tools.js';
import { LINEAR_NAMED_COMMANDS, LINEAR_NAMED_TOOLS, LINEAR_NAMED_BY_NAME } from '../src/linear-named-specs.js';
import { buildNamedOperation } from '../src/linear-named.js';
import { loadConfig } from '../src/config.js';
import { testEnv } from './helpers.mjs';
import * as S from '../src/seat-state.js';

function fixture(scopes = ['zulip:read', 'zulip:write']) {
  const store = S.memoryStore();
  const gate = Object.fromEntries(['reserve', 'auditCall', 'linearApiBegin', 'linearApiSet', 'linearApiDrop'].map((name) => [name, (args) => S[name](store, args)]));
  const requests = [];
  const linear = {
    secrets: () => [], confirmViewer: async () => ({ name: 'Grok Web', app: true }),
    async call(query, variables) {
      requests.push({ query, variables });
      if (linear.result) return linear.result;
      if (query.includes('AgentSyncLabel')) return { issueLabel: { id: variables.id, name: variables.id === 'claimed' ? 'claimed:CODEX' : 'ordinary' } };
      const root = parse(query).definitions.find((d) => d.kind === 'OperationDefinition').selectionSet.selections[0].name.value;
      return { [root]: { success: true, id: 'resource', project: { id: 'resource' } } };
    },
  };
  const tools = new HostedTools({ seat: 'GROK-WEB', scopes, config: loadConfig(testEnv()), zulip: null, key: '', gate,
    linear, linearWrites: true, sleep: async () => {}, log: () => {},
  });
  return { tools, requests, store, linear };
}

const argsFor = (entry) => Object.fromEntries(Object.entries(entry.args).filter(([, a]) => a.required).map(([name, a]) => [name,
  a.schema.type === 'object' ? {} : a.schema.type === 'boolean' ? true : a.schema.type === 'integer' ? 1 : 'resource',
]));

test('named tool catalog is unique, discoverable, closed and bound to the seat', () => {
  assert.ok(LINEAR_NAMED_TOOLS.length >= 140);
  assert.equal(new Set(LINEAR_NAMED_TOOLS.map((t) => t.name)).size, LINEAR_NAMED_TOOLS.length);
  for (const tool of LINEAR_NAMED_TOOLS) {
    assert.ok(HOSTED_TOOL_LIST.some((t) => t.name === tool.name));
    assert.equal(tool.inputSchema.additionalProperties, false);
    for (const identity of ['seat', 'by', 'api_key', 'client_secret']) assert.equal(Object.hasOwn(tool.inputSchema.properties, identity), false);
    if (!tool.annotations.readOnlyHint) assert.ok(tool.inputSchema.required.includes('idempotency_key'));
  }
});

test('every named adapter builds the expected operation and retains required variable types', () => {
  for (const entry of LINEAR_NAMED_COMMANDS) {
    const args = argsFor(entry);
    const built = buildNamedOperation(entry.name, args);
    const operation = parse(built.document).definitions[0];
    assert.equal(operation.operation, entry.write ? 'mutation' : 'query', entry.name);
    assert.equal(operation.selectionSet.selections.length, 1, entry.name);
    assert.equal(operation.selectionSet.selections[0].name.value, entry.root, entry.name);
    for (const definition of operation.variableDefinitions ?? []) {
      if (entry.args[definition.variable.name.value].required) assert.equal(definition.type.kind, 'NonNullType', entry.name);
    }
  }
});

test('named reads preserve filters and pagination and cannot escape response selection', async () => {
  const { tools, requests } = fixture();
  const result = await tools.call('linear_list_projects', { first: 3, after: 'cursor', filter: { name: { contains: 'Fleet' } } });
  assert.notEqual(result.isError, true);
  assert.deepEqual(requests[0].variables, { first: 3, after: 'cursor', filter: { name: { contains: 'Fleet' } }, includeArchived: false });
  assert.equal(requests[0].query.includes('projects'), true);
  const before = requests.length;
  const attack = await tools.call('linear_get_project', { id: 'resource', selection: 'id } } mutation X { projectDelete(id:"x") { success } } fragment Y on X { id' });
  assert.equal(attack.isError, true);
  assert.equal(requests.length, before);
});

test('save aliases route to create/update and preserve body attribution and idempotency', async () => {
  const { tools, requests } = fixture();
  const input = { name: 'Example', description: 'Details' };
  const create = await tools.call('linear_save_project', { input, idempotency_key: 'save-new' });
  assert.notEqual(create.isError, true);
  assert.match(requests[0].query, /projectCreate/);
  assert.match(requests[0].variables.input.description, /^\[GROK-WEB/);
  assert.equal(input.description, 'Details');
  await tools.call('linear_save_project', { input, idempotency_key: 'save-new' });
  assert.equal(requests.length, 1, 'the same mutation is not sent twice');
  await tools.call('linear_save_project', { id: 'resource', input, idempotency_key: 'save-existing' });
  assert.match(requests[1].query, /projectUpdate/);
});

test('named mutation scopes and fleet-label retire/restore guards remain enforced', async () => {
  const readOnly = fixture(['zulip:read']);
  const refused = await readOnly.tools.call('linear_delete_comment', { id: 'resource', idempotency_key: 'delete-comment' });
  assert.equal(refused.isError, true);
  assert.equal(readOnly.requests.length, 0);
  const { tools, requests } = fixture();
  const protectedLabel = await tools.call('linear_retire_issue_label', { id: 'claimed', idempotency_key: 'retire-claimed' });
  assert.equal(protectedLabel.isError, true);
  assert.equal(requests.filter((r) => r.query.includes('issueLabelRetire')).length, 0);
  const allowed = await tools.call('linear_restore_issue_label', { id: 'ordinary', idempotency_key: 'restore-ordinary' });
  assert.notEqual(allowed.isError, true);
  assert.equal(requests.filter((r) => r.query.includes('issueLabelRestore')).length, 1);
});

test('screenshot-oriented synonyms are available without private diff APIs', () => {
  for (const name of ['linear_list_issue_statuses', 'linear_get_issue_status', 'linear_list_milestones', 'linear_get_notifications', 'linear_get_status_updates', 'linear_save_milestone', 'linear_save_status_update', 'linear_mark_notification']) {
    assert.ok(LINEAR_NAMED_BY_NAME[name], name);
  }
  assert.equal(LINEAR_NAMED_BY_NAME.linear_merge_diff, undefined);
});


test('upload preparation returns private signed metadata without logging its payload', async () => {
  const { tools, requests, store, linear } = fixture();
  linear.result = { fileUpload: { success: true, uploadFile: { uploadUrl: 'https://storage.example.test/upload?X-Goog-Signature=unsigned-fixture', assetUrl: 'https://uploads.example.test/asset', headers: [{ key: 'Content-Type', value: 'image/png' }], contentType: 'image/png', filename: 'diagram.png', size: 42 } } };
  const response = await tools.call('linear_prepare_attachment_upload', { contentType: 'image/png', filename: 'diagram.png', size: 42, idempotency_key: 'prepare-upload' });
  assert.notEqual(response.isError, true);
  assert.match(response.content[0].text, /storage.example.test/);
  assert.equal(Object.hasOwn(requests[0].variables, 'makePublic'), false);
  assert.doesNotMatch(JSON.stringify(await S.callTail(store)), /storage.example.test|unsigned-fixture|diagram.png/);
});


test('save aliases reject empty IDs and incompatible create-only arguments', async () => {
  const { tools, requests } = fixture();
  const result = await tools.call('linear_save_project', { id: '', input: { name: 'Example' }, idempotency_key: 'empty-id' });
  assert.equal(result.isError, true);
  assert.equal(requests.length, 0);
  const create = LINEAR_NAMED_BY_NAME.linear_save_project.variants.create;
  const update = LINEAR_NAMED_BY_NAME.linear_save_project.variants.update;
  const extra = Object.keys(create.args).find((name) => !Object.hasOwn(update.args, name));
  if (extra) {
    assert.throws(() => buildNamedOperation('linear_save_project', { id: 'resource', input: {}, [extra]: 'unsupported' }), /not supported for this save operation/);
  }
});


test('attachment-from-upload alias consumes the previously uploaded asset URL', async () => {
  const { tools, requests } = fixture();
  const result = await tools.call('linear_create_attachment_from_upload', { input: { issueId: 'issue', url: 'https://uploads.example.test/asset', title: 'Uploaded asset' }, idempotency_key: 'attach-uploaded' });
  assert.notEqual(result.isError, true);
  assert.match(requests[0].query, /attachmentCreate/);
  assert.equal(requests[0].variables.input.url, 'https://uploads.example.test/asset');
  assert.match(LINEAR_NAMED_BY_NAME.linear_create_attachment_from_upload.description, /already uploaded asset/);
  assert.match(LINEAR_NAMED_BY_NAME.linear_create_attachment_from_upload.description, /does not upload file bytes/);
});
