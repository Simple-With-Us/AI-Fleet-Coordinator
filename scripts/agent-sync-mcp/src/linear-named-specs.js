// Named Linear tools are thin adapters over the existing GraphQL policy path.
import { LINEAR_READ_COMMANDS } from './linear-read-catalog.js';
import { LINEAR_WRITE_COMMANDS } from './linear-write-catalog.js';

const outputLimit = { type: 'integer', minimum: 1000, maximum: 64000, default: 24000 };
const selection = { type: 'string', minLength: 1, maxLength: 12000, description: 'Optional GraphQL response fields.  Omit to use the resource-specific default; discover fields with linear_schema.' };
const idem = { type: 'string', minLength: 1, maxLength: 100, pattern: '^[A-Za-z0-9_.:-]+$' };
const session = { type: 'string', maxLength: 80, pattern: '^[A-Za-z0-9_.:-]+$' };

function aliases(writes) {
  const out = [];
  for (const create of writes.filter((c) => c.name.startsWith('linear_create_'))) {
    const resource = create.name.slice('linear_create_'.length);
    const update = writes.find((c) => c.name === `linear_update_${resource}`);
    if (!update || !create.args.input || !update.args.input || !update.args.id) continue;
    out.push({ ...create, name: `linear_save_${resource}`, title: `Linear Save ${resource.split('_').map((word) => word[0].toUpperCase() + word.slice(1)).join(' ')}`,
      description: `Create or update a ${resource.replaceAll('_', ' ')}.  Supply id to update; omit id to create.`,
      args: { ...create.args, id: { ...update.args.id, required: false, type: update.args.id.type.replace(/!$/, '') } },
      variants: { create, update },
    });
  }
  for (const command of writes) {
    if (command.name.startsWith('linear_archive_') && command.name.endsWith('_label')) {
      out.push({ ...command, name: command.name.replace('linear_archive_', 'linear_retire_') });
    }
    if (command.name.startsWith('linear_unarchive_') && command.name.endsWith('_label')) {
      out.push({ ...command, name: command.name.replace('linear_unarchive_', 'linear_restore_') });
    }
  }
  return out;
}

const READ_ALIASES = {
  linear_list_issue_statuses: 'linear_list_workflow_states',
  linear_get_issue_status: 'linear_get_workflow_state',
  linear_list_milestones: 'linear_list_project_milestones',
  linear_get_milestone: 'linear_get_project_milestone',
  linear_get_notifications: 'linear_list_notifications',
  linear_get_status_updates: 'linear_list_project_updates',
};
const WRITE_ALIASES = {
  linear_save_milestone: 'linear_save_project_milestone',
  linear_save_status_update: 'linear_save_project_update',
  linear_delete_status_update: 'linear_delete_project_update',
  linear_mark_notification: 'linear_update_notification',
  linear_create_attachment_from_upload: 'linear_create_attachment',
};
function synonymCommands(commands, names) {
  return Object.entries(names).flatMap(([name, target]) => {
    const original = commands.find((c) => c.name === target);
    return original ? [{ ...original, name, description: name === 'linear_create_attachment_from_upload'
      ? 'Create an attachment record for an already uploaded asset.  Set input.url to the assetUrl from linear_prepare_attachment_upload and input.issueId to the target issue.  This does not upload file bytes.'
      : `${original.description}  Alias of ${target}.` }] : [];
  });
}
const writes = [...LINEAR_WRITE_COMMANDS, ...aliases(LINEAR_WRITE_COMMANDS)];
export const LINEAR_NAMED_COMMANDS = Object.freeze([
  ...[...LINEAR_READ_COMMANDS, ...synonymCommands(LINEAR_READ_COMMANDS, READ_ALIASES)].map((c) => ({ ...c, write: false })),
  ...[...writes, ...synonymCommands(writes, WRITE_ALIASES)].map((c) => ({ ...c, write: true })),
]);
export const LINEAR_NAMED_BY_NAME = Object.freeze(Object.fromEntries(LINEAR_NAMED_COMMANDS.map((c) => [c.name, c])));
export const LINEAR_NAMED_TOOLS = Object.freeze(LINEAR_NAMED_COMMANDS.map((command) => {
  const properties = Object.fromEntries(Object.entries(command.args).map(([name, arg]) => [name, { ...arg.schema, ...(arg.description ? { description: arg.description } : {}), ...(name === "id" ? { minLength: 1 } : {}) }]));
  properties.max_chars = outputLimit;
  if (command.selection) properties.selection = selection;
  const required = Object.entries(command.args).filter(([, arg]) => arg.required).map(([name]) => name);
  if (command.write) {
    properties.idempotency_key = idem;
    properties.session = session;
    required.push('idempotency_key');
  }
  return {
    name: command.name, title: command.title,
    description: `${command.description}  Uses this seat's Linear app and existing API limits.${command.write ? '  Requires a unique idempotency key; unknown outcomes are never replayed automatically.  Fleet ownership labels remain protected.' : ''}`,
    inputSchema: { type: 'object', additionalProperties: false, properties, ...(required.length ? { required } : {}) },
    annotations: { readOnlyHint: !command.write, destructiveHint: command.write && Boolean(command.destructive), openWorldHint: true },
  };
}));
