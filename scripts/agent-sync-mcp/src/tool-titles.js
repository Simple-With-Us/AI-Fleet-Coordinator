// Display labels only. Protocol names and all execution metadata stay unchanged.
const WORDS = Object.freeze({ pr: 'Pull Request', prs: 'Pull Requests', api: 'API', url: 'URL', urls: 'URLs', git: 'Git', id: 'ID', ids: 'IDs', sha: 'SHA', graphql: 'GraphQL' });
function words(value) {
  return value.split('_').map(word => WORDS[word] ?? (word[0]?.toUpperCase() + word.slice(1))).join(' ');
}
export function withServiceTitle(tool) {
  const family = tool.name.startsWith('linear_') ? 'Linear' : tool.name.startsWith('gr_') ? 'GitHub' : null;
  if (!family) return tool;
  const suffix = tool.name.replace(/^(?:linear_|gr_(?:wf_|v1_)?)/, '');
  const label = (tool.title || words(suffix)).replace(/^(?:Linear|GitHub|Gr)(?::\s*|\s+)/, '');
  return { ...tool, title: `${family}: ${label}` };
}
