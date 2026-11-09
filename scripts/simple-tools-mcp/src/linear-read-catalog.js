// Named read commands for the official Linear GraphQL Query schema.
// Selections are deliberately small; linear_query remains available for fields
// not covered here, and the command layer may accept a caller selection override.

const stringArg = (description, required = false) => ({
  type: "String",
  required,
  description,
  schema: { type: "string", ...(required ? { minLength: 1 } : {}) },
});

const firstArg = () => ({
  type: "Int",
  required: false,
  description: "Maximum number of records to return (1–100; defaults to 20).",
  default: 20,
  schema: { type: "integer", minimum: 1, maximum: 100, default: 20 },
});

const filterArg = (type, description = "Linear filter object for this resource.") => ({
  type,
  required: false,
  description,
  schema: { type: "object", additionalProperties: true },
});

const listArgs = (filterType = null) => ({
  first: firstArg(),
  after: stringArg("Opaque pagination cursor returned as endCursor by the previous page."),
  ...(filterType ? { filter: filterArg(filterType) } : {}),
  includeArchived: {
    type: "Boolean",
    required: false,
    description: "Include archived records when supported by the resource.",
    default: false,
    schema: { type: "boolean", default: false },
  },
});

const byId = (description = "Linear resource identifier.") => ({ id: stringArg(description, true) });
const releaseAvailability = "Requires a Linear plan or workspace configuration that enables Releases.";

const connectionSelection = (nodes) => `nodes { ${nodes} } pageInfo { hasNextPage endCursor }`;
const list = (name, title, description, root, filterType, selection) => ({
  name,
  title,
  description,
  root,
  args: listArgs(filterType),
  selection: connectionSelection(selection),
});
const get = (name, title, description, root, selection, idDescription) => ({
  name,
  title,
  description,
  root,
  args: byId(idDescription),
  selection,
});

export const LINEAR_READ_COMMANDS = [
  list("linear_list_issues", "List Issues", "List issues with status, ownership, and project context.", "issues", "IssueFilter", "id identifier title url createdAt updatedAt priority estimate dueDate state { id name type } assignee { id name } team { id name key } project { id name }"),
  get("linear_get_issue", "Get Issue", "Retrieve one issue by ID or identifier.", "issue", "id identifier title description url createdAt updatedAt priority estimate dueDate state { id name type } assignee { id name } team { id name key } project { id name } cycle { id name number }", "Issue ID or identifier."),
  list("linear_list_comments", "List Comments", "List comments, optionally filtered by issue or another supported comment field.", "comments", "CommentFilter", "id body createdAt updatedAt issue { id identifier title } user { id name }"),
  { name: "linear_get_comment", title: "Get Comment", description: "Retrieve one comment by ID.", root: "comment", args: { id: stringArg("Comment ID.", true) }, selection: "id body createdAt updatedAt issue { id identifier title } user { id name }" },
  list("linear_list_attachments", "List Attachments", "List attachments and their linked issues.", "attachments", "AttachmentFilter", "id title subtitle url source sourceType createdAt updatedAt issue { id identifier title }"),
  get("linear_get_attachment", "Get Attachment", "Retrieve one attachment by ID.", "attachment", "id title subtitle url source sourceType createdAt updatedAt issue { id identifier title }", "Attachment ID."),
  list("linear_list_cycles", "List Cycles", "List cycles with their team and date range.", "cycles", "CycleFilter", "id name number description startsAt endsAt completedAt isActive team { id name key }"),
  get("linear_get_cycle", "Get Cycle", "Retrieve one cycle by ID.", "cycle", "id name number description startsAt endsAt completedAt isActive team { id name key }", "Cycle ID."),
  list("linear_list_custom_views", "List Custom Views", "List custom views available to the authenticated Linear account.", "customViews", "CustomViewFilter", "id name description modelName shared slugId team { id name key }"),
  get("linear_get_custom_view", "Get Custom View", "Retrieve one custom view by ID.", "customView", "id name description modelName shared slugId filterData team { id name key }", "Custom view ID."),
  list("linear_list_documents", "List Documents", "List documents with title, summary, URL, and parent context.", "documents", "DocumentFilter", "id title summary url createdAt updatedAt team { id name key }"),
  get("linear_get_document", "Get Document", "Retrieve one document by ID, including its content.", "document", "id title summary content url createdAt updatedAt team { id name key }", "Document ID."),
  list("linear_list_workflow_states", "List Workflow States", "List issue workflow states and their teams.", "workflowStates", "WorkflowStateFilter", "id name description type color position team { id name key }"),
  get("linear_get_workflow_state", "Get Workflow State", "Retrieve one workflow state by ID.", "workflowState", "id name description type color position team { id name key }", "Workflow state ID."),
  get("linear_get_triage_responsibility", "Get Triage Responsibility", "Retrieve one triage responsibility by ID.", "triageResponsibility", "id action manualSelection { assignmentIndex userIds } team { id name key } currentUser { id name } timeSchedule { id name }", "Triage responsibility ID."),
  list("linear_list_issue_labels", "List Issue Labels", "List issue labels with their team and group metadata.", "issueLabels", "IssueLabelFilter", "id name description color groupType isGroup team { id name key }"),
  get("linear_get_issue_label", "Get Issue Label", "Retrieve one issue label by ID.", "issueLabel", "id name description color groupType isGroup team { id name key }", "Issue label ID."),
  list("linear_list_project_labels", "List Project Labels", "List project labels with their team and group metadata.", "projectLabels", "ProjectLabelFilter", "id name description color groupType isGroup team { id name key }"),
  get("linear_get_project_label", "Get Project Label", "Retrieve one project label by ID.", "projectLabel", "id name description color groupType isGroup team { id name key }", "Project label ID."),
  list("linear_list_projects", "List Projects", "List projects with status, health, and schedule details.", "projects", "ProjectFilter", "id name description url state health startDate targetDate createdAt updatedAt"),
  get("linear_get_project", "Get Project", "Retrieve one project by ID.", "project", "id name description content url state health startDate targetDate createdAt updatedAt", "Project ID."),
  list("linear_list_project_milestones", "List Project Milestones", "List project milestones and their target dates.", "projectMilestones", "ProjectMilestoneFilter", "id name description targetDate sortOrder project { id name }"),
  get("linear_get_project_milestone", "Get Project Milestone", "Retrieve one project milestone by ID.", "projectMilestone", "id name description targetDate sortOrder project { id name }", "Project milestone ID."),
  list("linear_list_teams", "List Teams", "List teams visible to the authenticated Linear account.", "teams", "TeamFilter", "id name displayName key description cyclesEnabled"),
  get("linear_get_team", "Get Team", "Retrieve one team by ID or key.", "team", "id name displayName key description cyclesEnabled", "Team ID or key."),
  list("linear_list_users", "List Users", "List users visible to the authenticated Linear account.", "users", "UserFilter", "id name displayName active admin guest"),
  get("linear_get_user", "Get User", "Retrieve one user by ID.", "user", "id name displayName active admin guest", "User ID."),
  { name: "linear_get_workspace", title: "Get Workspace", description: "Retrieve the authenticated workspace's public name and identifier.", root: "organization", args: {}, selection: "id name createdAt" },
  { name: "linear_list_templates", title: "List Templates", description: "List templates available to the authenticated Linear account.", root: "templates", args: {}, selection: "id name description type updatedAt team { id name key }" },
  { name: "linear_search_templates", title: "Search Templates", description: "Find templates matching a Linear template filter.", root: "templateSearch", args: { filter: filterArg("TemplateFilter"), first: firstArg(), includeArchived: { type: "Boolean", required: false, default: false, description: "Include archived templates.", schema: { type: "boolean", default: false } } }, selection: "id name description type updatedAt team { id name key }" },
  get("linear_get_template", "Get Template", "Retrieve one template by ID, including its template data.", "template", "id name description type templateData updatedAt team { id name key }", "Template ID."),
  list("linear_list_notifications", "List Notifications", "List notifications visible to the authenticated user.", "notifications", "NotificationFilter", "id type title subtitle createdAt readAt url"),
  get("linear_get_notification", "Get Notification", "Retrieve one notification by ID.", "notification", "id type title subtitle createdAt readAt url", "Notification ID."),
  list("linear_list_project_updates", "List Project Updates", "List project updates with health and linked project.", "projectUpdates", "ProjectUpdateFilter", "id body health createdAt updatedAt project { id name }"),
  get("linear_get_project_update", "Get Project Update", "Retrieve one project update by ID.", "projectUpdate", "id body health createdAt updatedAt project { id name }", "Project update ID."),
  list("linear_list_initiatives", "List Initiatives", "List initiatives with owner, health, and target date.", "initiatives", "InitiativeFilter", "id name description status health targetDate createdAt updatedAt owner { id name }"),
  get("linear_get_initiative", "Get Initiative", "Retrieve one initiative by ID.", "initiative", "id name description status health targetDate createdAt updatedAt owner { id name }", "Initiative ID."),
  list("linear_list_initiative_updates", "List Initiative Updates", "List initiative updates with health and parent initiative.", "initiativeUpdates", "InitiativeUpdateFilter", "id body health createdAt updatedAt initiative { id name } user { id name }"),
  get("linear_get_initiative_update", "Get Initiative Update", "Retrieve one initiative update by ID.", "initiativeUpdate", "id body health createdAt updatedAt initiative { id name } user { id name }", "Initiative update ID."),
  list("linear_list_favorites", "List Favorites", "List favorites for the authenticated user.", "favorites", null, "id title type createdAt updatedAt"),
  get("linear_get_favorite", "Get Favorite", "Retrieve one favorite by ID.", "favorite", "id title type createdAt updatedAt", "Favorite ID."),
  list("linear_list_roadmaps", "List Roadmaps", "List roadmaps with owner and workspace details.", "roadmaps", null, "id name description url createdAt updatedAt owner { id name }"),
  get("linear_get_roadmap", "Get Roadmap", "Retrieve one roadmap by ID.", "roadmap", "id name description url createdAt updatedAt owner { id name }", "Roadmap ID."),
  list("linear_list_customer_needs", "List Customer Needs", "List customer needs (Linear's customer request records) with linked customer and issue.", "customerNeeds", "CustomerNeedFilter", "id body createdAt updatedAt customer { id name } issue { id identifier title }"),
  get("linear_get_customer_need", "Get Customer Need", "Retrieve one customer need by ID.", "customerNeed", "id body createdAt updatedAt customer { id name } issue { id identifier title }", "Customer need ID."),
  list("linear_list_customers", "List Customers", "List customer records with their key contact and status metadata.", "customers", "CustomerFilter", "id name domains createdAt updatedAt"),
  get("linear_get_customer", "Get Customer", "Retrieve one customer by ID.", "customer", "id name domains createdAt updatedAt", "Customer ID."),
  list("linear_list_releases", "List Releases", `List releases with their pipeline and stage.  ${releaseAvailability}`, "releases", "ReleaseFilter", "id name slugId version description createdAt updatedAt targetDate startDate url stage { id name type } pipeline { id name type }"),
  get("linear_get_release", "Get Release", `Retrieve one release by ID or slug.  ${releaseAvailability}`, "release", "id name slugId version description createdAt updatedAt targetDate startDate url stage { id name type } pipeline { id name type } releaseNote { id title }", "Release ID or slug."),
  list("linear_list_release_notes", "List Release Notes", `List release notes with linked releases.  ${releaseAvailability}`, "releaseNotes", "ReleaseNoteFilter", "id title url generationStatus releaseCount createdAt updatedAt pipeline { id name }"),
  get("linear_get_release_note", "Get Release Note", `Retrieve one release note by ID or slug.  ${releaseAvailability}`, "releaseNote", "id title url generationStatus releaseCount createdAt updatedAt pipeline { id name }", "Release note ID or slug."),
  list("linear_list_release_pipelines", "List Release Pipelines", `List release pipelines and their release settings.  ${releaseAvailability}`, "releasePipelines", "ReleasePipelineFilter", "id name slugId type isProduction approximateReleaseCount createdAt updatedAt url"),
  get("linear_get_release_pipeline", "Get Release Pipeline", `Retrieve one release pipeline by ID or slug.  ${releaseAvailability}`, "releasePipeline", "id name slugId type isProduction approximateReleaseCount createdAt updatedAt url", "Release pipeline ID or slug."),
  list("linear_list_release_stages", "List Release Stages", `List release stages and their parent pipelines.  ${releaseAvailability}`, "releaseStages", "ReleaseStageFilter", "id name color frozen position type createdAt updatedAt pipeline { id name }"),
  get("linear_get_release_stage", "Get Release Stage", `Retrieve one release stage by ID.  ${releaseAvailability}`, "releaseStage", "id name color frozen position type createdAt updatedAt pipeline { id name }", "Release stage ID."),
  list("linear_list_agent_skills", "List Agent Skills", "List agent skills visible to the authenticated Linear account.  Agent skills are a Developer Preview feature.", "agentSkills", "AgentSkillFilter", "id title description shared teamId createdAt updatedAt owner { id name }"),
  get("linear_get_agent_skill", "Get Agent Skill", "Retrieve one agent skill, including its body.  Agent skills are a Developer Preview feature.", "agentSkill", "id title description body shared teamId createdAt updatedAt owner { id name }", "Agent skill ID."),
];

export const LINEAR_READ_CATALOG_NOTES = [
  "The current official schema exposes customerNeeds/customerNeed for customer requests; it has no customerRequests root.",
  "Milestones are exposed as projectMilestones/projectMilestone.",
  "The workspace query is organization; templates is a list-valued root with no pagination arguments.",
  "The comment root accepts optional id/hash arguments in GraphQL; the named linear_get_comment command requires id for a simpler bounded input contract.",
  "The current official schema includes releases and agent skills; releases can depend on plan availability and agent skills are Developer Preview.",
  "The diff query is marked internal in the official schema and is excluded. Documentation search and image extraction are not GraphQL query roots.",
  "Default selections are intentionally small. Use linear_query with a read-only operation for other public fields supported by the current schema.",
];
