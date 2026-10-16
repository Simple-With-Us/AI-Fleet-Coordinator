// A fake Linear API for the hosted tools, as a `fetch` function.  No
// dependencies, so it runs under CI's plain `node --test` and as the workerd
// flow's outbound service.  It implements only what the seven Linear tools
// call:  the OAuth token endpoint, and the GraphQL documents Q_WHOAMI,
// Q_TEAMS, Q_LABELS, Q_ISSUE, Q_COMMENTS, Q_LIST and the five mutations.
//
// Every request is recorded in one ordered list, so a test can count mints,
// read the Authorization header a call carried, and assert that no mutation
// was sent at all.  The viewer is the seat's own app by default;  `viewer`
// overrides it, which is how the swapped-credentials check gets a wrong name,
// a non-app viewer, or another seat's app.
//
// The seeded workspace is small but real-shaped:  two teams carrying the states
// board's status mapping names (board:202-213), the four flat labels of
// FLAT_LABELS (board:226), a claim and a seat label for the hosted seat, and
// the Severity, Kind and Env single-select groups.  Tests add their own issues.

export const GRAPHQL_URL = "https://api.linear.app/graphql";
export const TOKEN_URL = "https://api.linear.app/oauth/token";
export const SCOPE = "read,write,app:assignable,app:mentionable";
export const WORKSPACE_URL_KEY = "simple-with-us";
const LINEAR_PROFILE_URL_RE = /https:\/\/linear\.app\/[a-z0-9-]+\/profiles\//i;
export const STATE_NAMES = Object.freeze(["Todo", "Backlog", "In Progress", "In Review", "Done", "Deployed", "Won't Fix", "Parked", "Canceled", "Duplicate"]);

const slug = (value) => String(value).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
const uniq = (value) => `${slug(value).slice(0, 40)}-${randomHex(6)}`;

/** Hexadecimal noise, so no two fake tokens and no two fake ids are equal. */
function randomHex(bytes) {
  const out = new Uint8Array(bytes);
  crypto.getRandomValues(out);
  return [...out].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The operation name and kind of a GraphQL document, or null when it is neither. */
export function opOf(query) {
  const m = /\b(query|mutation)\s+(\w+)/.exec(String(query ?? ""));
  return m ? { kind: m[1], name: m[2] } : null;
}

const connection = (nodes) => ({ pageInfo: { hasNextPage: false, endCursor: null }, nodes });

export class FakeLinear {
  constructor({ viewer = { id: "user-grok-web", name: "Grok Web", displayName: "Grok Web", app: true }, expiresIn = 30 * 86_400, stamp = Date.parse("2026-10-09T10:00:00Z") } = {}) {
    this.viewer = { email: "grok-web@linear.test", ...viewer };
    this.expiresIn = expiresIn;
    this.stamp = stamp;
    this.issued = []; // every access token this fake has minted, in order
    this.requests = []; // mint and GraphQL alike, in the order they arrived
    this.unauthorized = 0; // answer this many GraphQL calls with a 401
    this.queue = []; // [{match: (entry) => bool, respond: (entry) => Response}]
    this.teams = [];
    this.labels = [];
    this.issues = [];
    this.relations = [];
    this.fetch = this.fetch.bind(this);
    seedWorkspace(this);
  }

  // ---------------------------------------------------------------- the workspace

  addTeam(key, name, stateNames = STATE_NAMES) {
    const team = { id: uniq(key), key, name, states: stateNames.map((s) => ({ id: uniq(`${key}-${s}`), name: s, type: "unstarted" })) };
    this.teams.push(team);
    return team;
  }

  /** A label node, as Linear returns it:  a flat label, a grouped one or a group itself. */
  addLabel(name, { isGroup = false, parent = null, team = null } = {}) {
    const label = { id: uniq(name), name, isGroup, parent, team };
    this.labels.push(label);
    return label;
  }

  /** An issue, with the parts every query in this module selects. */
  addIssue({ id = null, identifier = "AFC-1", title = "An issue", team = "AFC", state = "Todo", labels = [], description = "", priority = 0, url = null } = {}) {
    const row = {
      id: id ?? uniq(identifier),
      identifier,
      title,
      url: url ?? `https://linear.app/simple-with-us/issue/${identifier}`,
      priority,
      description,
      createdAt: new Date(this.stamp).toISOString(),
      updatedAt: new Date(this.stamp).toISOString(),
      state: this.stateNode(String(team), state),
      team: this.teamNode(team),
      labels: labels.map((name) => ({ id: this.labelNode(name).id, name: String(name), parent: null })),
      comments: [],
    };
    this.issues.push(row);
    return row;
  }

  teamNode(key) {
    const team = typeof key === "object" && key !== null ? this.teams.find((t) => t.key === key.key) ?? this.addTeam(key.key, key.name ?? key.key) : this.teams.find((t) => t.key === key);
    return { id: team.id, key: team.key, name: team.name };
  }

  stateNode(teamKey, name) {
    const team = this.teams.find((t) => t.key === teamKey) ?? this.teams[0];
    const state = team.states.find((s) => s.name === name) ?? team.states[0];
    return { id: state.id, name: state.name, type: state.type };
  }

  labelNode(name) {
    const label = this.labels.find((l) => l.name === name);
    if (!label) throw new Error(`fake linear: no label named ${JSON.stringify(name)}`);
    return label;
  }

  teamId(key) {
    return this.teams.find((t) => t.key === key).id;
  }

  labelId(name) {
    return this.labelNode(name).id;
  }

  stateId(teamKey, name) {
    return this.teams.find((t) => t.key === teamKey).states.find((s) => s.name === name).id;
  }

  /** One stored issue, by id or by identifier, or null. */
  findIssue(id) {
    return this.issues.find((i) => i.id === id || i.identifier === id) ?? null;
  }

  // ---------------------------------------------------------------- recorded traffic

  mints() {
    return this.requests.filter((r) => r.kind === "mint");
  }

  calls() {
    return this.requests.filter((r) => r.kind === "graphql");
  }

  /** Every mutation sent, in order:  the empty list is the proof a read only. */
  mutations() {
    return this.calls().filter((r) => opOf(r.body.query)?.kind === "mutation");
  }

  /** Answer the next request matching `match(entry)` with `respond(entry)` instead of the normal route. */
  failNext(match, respond) {
    this.queue.push({ match, respond });
  }

  json(status, body, headers = {}) {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
  }

  async fetch(input, init = {}) {
    // Miniflare hands the outbound service its own Request class, so test the shape, not the class.
    const request = typeof input === "string" || input instanceof URL ? new Request(input, init) : input;
    const url = new URL(request.url);
    const isToken = url.origin === "https://api.linear.app" && url.pathname === "/oauth/token";
    if (!isToken && !(url.origin === "https://api.linear.app" && url.pathname === "/graphql")) return new Response("blocked in test", { status: 599 });
    const raw = await request.text();
    // The client hands in a plain header object;  a Headers instance works too.
    const headers = Object.fromEntries(init.headers ? Object.entries(init.headers) : request.headers);
    const entry = {
      kind: isToken ? "mint" : "graphql",
      url: request.url,
      method: request.method,
      headers,
      redirect: request.redirect,
      signal: request.signal,
      authorization: request.headers.get("authorization"),
      raw,
      body: isToken ? raw : safeJson(raw),
    };
    this.requests.push(entry);
    const queued = this.queue.findIndex((q) => q.match(entry));
    if (queued >= 0) return this.queue.splice(queued, 1)[0].respond(entry);
    return isToken ? this.mintResponse(entry) : this.graphqlResponse(entry);
  }

  mintResponse(entry) {
    if (this.mintDown) return this.json(503, { error: "temporarily_unavailable" });
    const params = new URLSearchParams(entry.raw);
    // Linear answers a mint with no usable credential in the body, and the
    // client turns that into a mint error naming nothing.
    if (!params.get("client_id") || !params.get("client_secret")) return this.json(400, { error: "invalid_client" });
    const accessToken = `fake-linear-token-${this.issued.length + 1}-${randomHex(12)}`;
    this.issued.push(accessToken);
    return this.json(200, { access_token: accessToken, token_type: "Bearer", scope: params.get("scope") ?? "", expires_in: this.expiresIn });
  }

  graphqlResponse(entry) {
    if (this.unauthorized > 0) {
      this.unauthorized -= 1;
      return this.unauthorizedResponse();
    }
    // A bearer this fake never minted is a 401, which is what the one retry
    // and the 24-hour rule are written against.
    if (!this.issued.includes(String(entry.authorization ?? "").replace(/^Bearer\s+/, ""))) return this.unauthorizedResponse();
    const op = opOf(entry.body?.query);
    if (!op) return this.json(200, { errors: [{ message: "fake linear: no operation in the document" }] });
    const variables = entry.body?.variables ?? {};
    if (op.kind === "mutation") {
      const outcome = this.mutation(op.name, variables);
      if (outcome?.graphqlError) return this.json(200, { data: null, errors: [outcome.graphqlError] });
      return this.json(200, { data: outcome });
    }
    return this.json(200, { data: this.query(op.name, variables) });
  }

  unauthorizedResponse() {
    return this.json(401, { errors: [{ message: "Authentication required" }] });
  }

  query(name, variables) {
    switch (name) {
      case "BoardWhoami":
        return { viewer: { ...this.viewer }, organization: { name: "Simple With Us", urlKey: WORKSPACE_URL_KEY } };
      case "BoardTeams":
        // Q_TEAMS selects `states(first: 50) { nodes { ... } }`, so the states
        // travel as a connection, exactly as Linear sends them.
        return { teams: connection(this.teams.map((t) => ({ id: t.id, key: t.key, name: t.name, states: connection(t.states) }))) };
      case "BoardLabels":
        return { issueLabels: connection(this.labels) };
      case "BoardIssue":
        return { issue: this.issueNode(this.findIssue(variables.id)) };
      case "BoardComments":
        return { issue: { comments: connection(this.findIssue(variables.id)?.comments ?? []) } };
      case "BoardList":
        return { issues: connection(this.listNodes(variables.filter ?? {})) };
      default:
        return null;
    }
  }

  mutation(name, variables) {
    switch (name) {
      case "BoardFile":
        return { issueCreate: this.createIssue(variables.input) };
      case "BoardUpdate":
        return { issueUpdate: this.updateIssue(variables.id, variables.input) };
      case "BoardComment": {
        const created = this.createComment(variables.input);
        if (created?.graphqlError) return { graphqlError: created.graphqlError };
        return { commentCreate: created };
      }
      case "BoardRelate":
        return { issueRelationCreate: this.createRelation(variables.input) };
      default:
        return null;
    }
  }

  /** One issue in the shape the module's queries select. */
  issueNode(row) {
    if (!row) return null;
    return { ...row, labels: connection(row.labels), relations: connection([]), inverseRelations: connection([]) };
  }

  /** The rows a list filter matches, so a filter in a test really narrows. */
  listNodes(filter) {
    return this.issues.filter((row) => {
      if (filter.team?.key?.eq && row.team.key !== filter.team.key.eq) return false;
      if (filter.state?.name?.in && !filter.state.name.in.includes(row.state.name)) return false;
      if (filter.priority?.eq !== undefined && row.priority !== filter.priority.eq) return false;
      const want = filter.labels?.some?.name?.eq;
      if (want && !row.labels.some((l) => l.name === want)) return false;
      return true;
    });
  }

  /** The workflow state a stateId names, whichever team owns it.  */
  stateById(stateId) {
    const found = this.teams.flatMap((t) => t.states).find((s) => s.id === stateId);
    return found ? { id: found.id, name: found.name, type: found.type } : { id: stateId, name: stateId, type: "unstarted" };
  }

  createIssue(input) {
    const team = this.teams.find((t) => t.id === input.teamId) ?? this.teams[0];
    const number = 300 + this.issues.length;
    const row = this.addIssue({ identifier: `${team.key}-${number}`, title: input.title, team, description: input.description ?? "", priority: input.priority ?? 0 });
    if (input.stateId) row.state = this.stateById(input.stateId);
    if (input.labelIds?.length) row.labels = input.labelIds.map((id) => ({ id, name: this.labelNode(this.labels.find((l) => l.id === id).name).name, parent: null }));
    return { success: true, issue: this.issueNode(row) };
  }

  /** Apply one issueUpdate input, so a later read sees what the write did. */
  updateIssue(id, input = {}) {
    const row = this.findIssue(id);
    if (!row) return { success: false, issue: null };
    if (input.stateId) row.state = this.stateById(input.stateId);
    const nameOf = (labelId) => this.labels.find((l) => l.id === labelId)?.name ?? labelId;
    for (const labelId of input.addedLabelIds ?? []) if (!row.labels.some((l) => l.id === labelId)) row.labels.push({ id: labelId, name: nameOf(labelId), parent: null });
    for (const labelId of input.removedLabelIds ?? []) row.labels = row.labels.filter((l) => l.id !== labelId);
    return { success: true, issue: this.issueNode(row) };
  }

  /** A comment with a caller-chosen id, as board's comment_id sends one. */
  createComment(input) {
    if (LINEAR_PROFILE_URL_RE.test(input.body ?? "")) {
      return {
        graphqlError: {
          message: "App user not valid",
          path: ["commentCreate"],
          extensions: { code: "INVALID_INPUT", userPresentableMessage: "App user not valid" },
        },
      };
    }
    const row = this.findIssue(input.issueId);
    const comment = { id: input.id, body: input.body, createdAt: new Date(this.stamp).toISOString(), user: { id: this.viewer.id, name: this.viewer.name, app: this.viewer.app } };
    // The same id updates the comment it names, which is what makes a retry
    // safe when the model repeats a verb that already posted.
    const at = row.comments.findIndex((c) => c.id === input.id);
    if (at >= 0) row.comments[at] = comment;
    else row.comments.push(comment);
    return { success: true, comment: { id: input.id } };
  }

  createRelation(input) {
    this.relations.push({ ...input });
    return { success: true, issueRelation: { id: input.id } };
  }
}

/** The two teams, the flat labels, and the grouped ones, every seeded workspace has. */
function seedWorkspace(fake) {
  fake.addTeam("AFC", "AI Fleet Coordinator");
  fake.addTeam("CC", "CodeCaps");
  for (const name of ["needs-owner", "ask-capture", "fleet-report", "stale-claim"]) fake.addLabel(name);
  // The claim and attribution labels:  reachable only through linear_claim.
  for (const seat of ["GROK-WEB", "CLAUDE"]) {
    fake.addLabel(`claimed:${seat}`);
    fake.addLabel(`seat:${seat}`);
  }
  fake.addLabel("app:wombat");
  fake.addLabel("app:fleet-infra");
  // Single-select groups:  Linear returns their members with a parent, so they
  // are never flat labels a seat may add.
  fake.addLabel("P0", { parent: { name: "Severity" } });
  fake.addLabel("agent-report", { parent: { name: "Kind" } });
  fake.addLabel("Mac", { parent: { name: "Env" } });
  fake.addLabel("Severity", { isGroup: true });
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}