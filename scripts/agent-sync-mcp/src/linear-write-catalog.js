// Curated named wrappers for common public Linear mutations.  Root names,
// argument types, and default selections were checked against Linear's public
// GraphQL schema at github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql.
const arg = (type, schema, required = false) => ({ type, required, schema });
const text = (required = false) => arg(required ? "String!" : "String", { type: "string" }, required);
const flag = () => arg("Boolean", { type: "boolean" });
const integer = (type, required, minimum, maximum) => arg(type, { type: "integer", minimum, maximum }, required);
const input = (type) => arg(`${type}!`, { type: "object", additionalProperties: true }, true);
const id = () => text(true);

const command = (name, title, root, args, selection, description, destructive = false) => ({
  name: `linear_${name}`,
  title,
  description,
  root,
  args,
  selection,
  destructive,
});

export const LINEAR_WRITE_COMMANDS = Object.freeze([
  command("create_issue", "Create Issue", "issueCreate", { input: input("IssueCreateInput") }, "success issue { id identifier title }", "Create a Linear issue."),
  command("update_issue", "Update Issue", "issueUpdate", { id: id(), input: input("IssueUpdateInput") }, "success issue { id identifier title }", "Update a Linear issue."),
  command("delete_issue", "Delete Issue", "issueDelete", { id: id() }, "success entity { id }", "Delete a Linear issue using Linear's normal deletion policy.", true),
  command("archive_issue", "Archive Issue", "issueArchive", { id: id(), trash: flag() }, "success entity { id }", "Archive a Linear issue.  Set trash only when moving it to trash is intended.", true),
  command("unarchive_issue", "Restore Issue", "issueUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear issue."),
  command("share_issue", "Share Issue", "issueShare", { id: id(), userId: id() }, "success issue { id identifier title }", "Share a Linear issue with a user.  Linear enforces record-level access permissions."),
  command("unshare_issue", "Unshare Issue", "issueUnshare", { id: id(), userId: id() }, "success issue { id identifier title }", "Remove a user's share access to a Linear issue.  Linear enforces record-level access permissions."),

  command("create_comment", "Create Comment", "commentCreate", { input: input("CommentCreateInput") }, "success comment { id body }", "Create a comment on a Linear issue."),
  command("update_comment", "Update Comment", "commentUpdate", { id: id(), input: input("CommentUpdateInput") }, "success comment { id body }", "Update a Linear comment."),
  command("delete_comment", "Delete Comment", "commentDelete", { id: id() }, "success", "Delete a Linear comment.", true),
  command("resolve_comment", "Resolve Comment", "commentResolve", { id: id(), resolvingCommentId: text() }, "success comment { id body }", "Resolve a Linear comment thread."),
  command("unresolve_comment", "Unresolve Comment", "commentUnresolve", { id: id() }, "success comment { id body }", "Reopen a resolved Linear comment thread."),

  command("create_document", "Create Document", "documentCreate", { input: input("DocumentCreateInput") }, "success document { id title }", "Create a Linear document."),
  command("update_document", "Update Document", "documentUpdate", { id: id(), input: input("DocumentUpdateInput") }, "success document { id title }", "Update a Linear document."),
  command("delete_document", "Delete Document", "documentDelete", { id: id() }, "success entity { id }", "Delete a Linear document.", true),
  command("unarchive_document", "Restore Document", "documentUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear document."),

  command("create_project", "Create Project", "projectCreate", {
    input: input("ProjectCreateInput"), slackChannelName: { ...text(), description: "Creates and links a Slack channel with this name." },
  }, "success project { id name }", "Create a Linear project."),
  command("update_project", "Update Project", "projectUpdate", { id: id(), input: input("ProjectUpdateInput") }, "success project { id name }", "Update a Linear project."),
  command("delete_project", "Delete Project", "projectDelete", { id: id() }, "success entity { id }", "Delete a Linear project.", true),
  command("archive_project", "Archive Project", "projectArchive", { id: id(), trash: flag() }, "success entity { id }", "Archive a Linear project.  Set trash only when moving it to trash is intended.", true),
  command("unarchive_project", "Restore Project", "projectUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear project."),

  command("create_issue_label", "Create Issue Label", "issueLabelCreate", { input: input("IssueLabelCreateInput") }, "success issueLabel { id name }", "Create a Linear issue label."),
  command("update_issue_label", "Update Issue Label", "issueLabelUpdate", { id: id(), input: input("IssueLabelUpdateInput") }, "success issueLabel { id name }", "Update a Linear issue label."),
  command("delete_issue_label", "Delete Issue Label", "issueLabelDelete", { id: id() }, "success", "Delete a Linear issue label.", true),
  command("retire_issue_label", "Retire Issue Label", "issueLabelRetire", { id: id() }, "success issueLabel { id name }", "Retire a Linear issue label.", true),
  command("restore_issue_label", "Restore Issue Label", "issueLabelRestore", { id: id() }, "success issueLabel { id name }", "Restore a retired Linear issue label."),
  command("create_project_label", "Create Project Label", "projectLabelCreate", { input: input("ProjectLabelCreateInput"), replaceTeamLabels: flag() }, "success projectLabel { id name }", "Create a Linear project label."),
  command("update_project_label", "Update Project Label", "projectLabelUpdate", { id: id(), input: input("ProjectLabelUpdateInput"), replaceTeamLabels: flag() }, "success projectLabel { id name }", "Update a Linear project label."),
  command("delete_project_label", "Delete Project Label", "projectLabelDelete", { id: id() }, "success", "Delete a Linear project label.", true),
  command("retire_project_label", "Retire Project Label", "projectLabelRetire", { id: id() }, "success projectLabel { id name }", "Retire a Linear project label.", true),
  command("restore_project_label", "Restore Project Label", "projectLabelRestore", { id: id() }, "success projectLabel { id name }", "Restore a retired Linear project label."),

  command("create_project_milestone", "Create Project Milestone", "projectMilestoneCreate", { input: input("ProjectMilestoneCreateInput") }, "success projectMilestone { id name }", "Create a milestone for a Linear project."),
  command("update_project_milestone", "Update Project Milestone", "projectMilestoneUpdate", { id: id(), input: input("ProjectMilestoneUpdateInput") }, "success projectMilestone { id name }", "Update a Linear project milestone."),
  command("delete_project_milestone", "Delete Project Milestone", "projectMilestoneDelete", { id: id() }, "success", "Delete a Linear project milestone.", true),

  command("update_cycle", "Update Cycle", "cycleUpdate", { id: id(), input: input("CycleUpdateInput") }, "success cycle { id name }", "Update a Linear cycle."),
  command("archive_cycle", "Archive Cycle", "cycleArchive", { id: id() }, "success entity { id }", "Archive a Linear cycle.", true),

  command("create_attachment", "Create Attachment", "attachmentCreate", { input: input("AttachmentCreateInput") }, "success attachment { id title url }", "Create an attachment record on a Linear issue.  This does not upload file bytes."),
  command("update_attachment", "Update Attachment", "attachmentUpdate", { id: id(), input: input("AttachmentUpdateInput") }, "success attachment { id title url }", "Update a Linear attachment record."),
  command("delete_attachment", "Delete Attachment", "attachmentDelete", { id: id() }, "success", "Delete a Linear attachment record.", true),
  command("link_attachment_url", "Link Attachment URL", "attachmentLinkURL", {
    issueId: id(), url: text(true), title: text(), id: text(),
  }, "success attachment { id title url }", "Link a URL as an attachment to a Linear issue."),
  command("prepare_attachment_upload", "Prepare Attachment Upload", "fileUpload", {
    contentType: text(true), filename: text(true), size: integer("Int!", true, 1, 2147483647),
  }, "success uploadFile { uploadUrl assetUrl headers { key value } contentType filename size }", "Prepare signed upload metadata only.  Send file bytes with PUT through the trusted client, and keep the signed URL and headers private; this command does not upload bytes or make the asset public."),

  command("create_project_update", "Create Project Update", "projectUpdateCreate", { input: input("ProjectUpdateCreateInput") }, "success projectUpdate { id health body }", "Create a Linear project update."),
  command("update_project_update", "Update Project Update", "projectUpdateUpdate", { id: id(), input: input("ProjectUpdateUpdateInput") }, "success projectUpdate { id health body }", "Update a Linear project update."),
  command("delete_project_update", "Delete Project Update", "projectUpdateDelete", { id: id() }, "success", "Delete a Linear project update.", true),
  command("archive_project_update", "Archive Project Update", "projectUpdateArchive", { id: id() }, "success entity { id }", "Archive a Linear project update.", true),
  command("unarchive_project_update", "Restore Project Update", "projectUpdateUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear project update."),

  command("create_custom_view", "Create Custom View", "customViewCreate", { input: input("CustomViewCreateInput") }, "success customView { id name }", "Create a Linear custom view."),
  command("update_custom_view", "Update Custom View", "customViewUpdate", { id: id(), input: input("CustomViewUpdateInput") }, "success customView { id name }", "Update a Linear custom view."),
  command("delete_custom_view", "Delete Custom View", "customViewDelete", { id: id() }, "success", "Delete a Linear custom view.", true),
  command("create_template", "Create Template", "templateCreate", { input: input("TemplateCreateInput") }, "success template { id name }", "Create a Linear template."),
  command("update_template", "Update Template", "templateUpdate", { id: id(), input: input("TemplateUpdateInput") }, "success template { id name }", "Update a Linear template."),
  command("delete_template", "Delete Template", "templateDelete", { id: id() }, "success", "Delete a Linear template.", true),
  command("update_notification", "Update Notification", "notificationUpdate", { id: id(), input: input("NotificationUpdateInput") }, "success notification { id category }", "Update a Linear notification."),
  command("archive_notification", "Archive Notification", "notificationArchive", { id: id() }, "success entity { id }", "Archive a Linear notification."),
  command("unarchive_notification", "Restore Notification", "notificationUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear notification."),

  command("create_issue_relation", "Create Issue Relation", "issueRelationCreate", { input: input("IssueRelationCreateInput") }, "success issueRelation { id type }", "Create a relation between Linear issues."),
  command("update_issue_relation", "Update Issue Relation", "issueRelationUpdate", { id: id(), input: input("IssueRelationUpdateInput") }, "success issueRelation { id type }", "Update a Linear issue relation."),
  command("delete_issue_relation", "Delete Issue Relation", "issueRelationDelete", { id: id() }, "success", "Delete a Linear issue relation.", true),

  command("create_initiative", "Create Initiative", "initiativeCreate", { input: input("InitiativeCreateInput") }, "success initiative { id name }", "Create a Linear initiative."),
  command("update_initiative", "Update Initiative", "initiativeUpdate", { id: id(), input: input("InitiativeUpdateInput") }, "success initiative { id name }", "Update a Linear initiative."),
  command("delete_initiative", "Delete Initiative", "initiativeDelete", { id: id() }, "success", "Delete a Linear initiative.", true),
  command("archive_initiative", "Archive Initiative", "initiativeArchive", { id: id() }, "success entity { id }", "Archive a Linear initiative.", true),
  command("unarchive_initiative", "Restore Initiative", "initiativeUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear initiative."),
  command("create_initiative_update", "Create Initiative Update", "initiativeUpdateCreate", { input: input("InitiativeUpdateCreateInput") }, "success initiativeUpdate { id health body }", "Create a Linear initiative update."),
  command("update_initiative_update", "Update Initiative Update", "initiativeUpdateUpdate", { id: id(), input: input("InitiativeUpdateUpdateInput") }, "success initiativeUpdate { id health body }", "Update a Linear initiative update."),
  command("archive_initiative_update", "Archive Initiative Update", "initiativeUpdateArchive", { id: id() }, "success entity { id }", "Archive a Linear initiative update.", true),
  command("unarchive_initiative_update", "Restore Initiative Update", "initiativeUpdateUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear initiative update."),
  command("create_initiative_relation", "Create Initiative Relation", "initiativeRelationCreate", { input: input("InitiativeRelationCreateInput") }, "success initiativeRelation { id sortOrder }", "Create a parent-child relation between Linear initiatives."),
  command("update_initiative_relation", "Update Initiative Relation", "initiativeRelationUpdate", { id: id(), input: input("InitiativeRelationUpdateInput") }, "success initiativeRelation { id sortOrder }", "Update a Linear initiative relation."),
  command("delete_initiative_relation", "Delete Initiative Relation", "initiativeRelationDelete", { id: id() }, "success", "Delete a Linear initiative relation.", true),
  command("add_initiative_project", "Add Initiative Project", "initiativeToProjectCreate", { input: input("InitiativeToProjectCreateInput") }, "success initiativeToProject { id sortOrder }", "Associate a Linear project with an initiative."),
  command("update_initiative_project", "Update Initiative Project", "initiativeToProjectUpdate", { id: id(), input: input("InitiativeToProjectUpdateInput") }, "success initiativeToProject { id sortOrder }", "Update a Linear initiative-to-project association."),
  command("remove_initiative_project", "Remove Initiative Project", "initiativeToProjectDelete", { id: id() }, "success", "Remove a project from a Linear initiative.", true),

  command("create_workflow_state", "Create Workflow State", "workflowStateCreate", { input: input("WorkflowStateCreateInput") }, "success workflowState { id name }", "Create a Linear workflow state."),
  command("update_workflow_state", "Update Workflow State", "workflowStateUpdate", { id: id(), input: input("WorkflowStateUpdateInput") }, "success workflowState { id name }", "Update a Linear workflow state."),
  command("archive_workflow_state", "Archive Workflow State", "workflowStateArchive", { id: id() }, "success entity { id }", "Archive a Linear workflow state.  Linear requires its issues to be archived first.", true),
  command("complete_release", "Complete Release", "releaseComplete", { input: input("ReleaseCompleteInput") }, "success release { id name }", "Mark a Linear release as completed.  The server still enforces release-plan permissions.", true),

  command("create_release", "Create Release", "releaseCreate", { input: input("ReleaseCreateInput") }, "success release { id name url }", "Create a Linear release.  The server still enforces release-plan permissions."),
  command("update_release", "Update Release", "releaseUpdate", { id: id(), input: input("ReleaseUpdateInput") }, "success release { id name url }", "Update a Linear release.  The server still enforces release-plan permissions."),
  command("delete_release", "Delete Release", "releaseDelete", { id: id() }, "success entity { id }", "Delete a Linear release.  The server still enforces release-plan permissions.", true),
  command("archive_release", "Archive Release", "releaseArchive", { id: id() }, "success entity { id }", "Archive a Linear release.  The server still enforces release-plan permissions.", true),
  command("unarchive_release", "Restore Release", "releaseUnarchive", { id: id() }, "success entity { id }", "Restore an archived Linear release.  The server still enforces release-plan permissions."),
  command("create_release_note", "Create Release Note", "releaseNoteCreate", { input: input("ReleaseNoteCreateInput") }, "success releaseNote { id title url }", "Create a Linear release note.  The server still enforces release-plan permissions."),
  command("update_release_note", "Update Release Note", "releaseNoteUpdate", { id: id(), input: input("ReleaseNoteUpdateInput") }, "success releaseNote { id title url }", "Update a Linear release note.  The server still enforces release-plan permissions."),
  command("delete_release_note", "Delete Release Note", "releaseNoteDelete", { id: id() }, "success", "Delete a Linear release note.  The server still enforces release-plan permissions.", true),
  command("create_agent_skill", "Create Agent Skill", "agentSkillCreate", { input: input("AgentSkillCreateInput") }, "success agentSkill { id title }", "Create a Linear agent skill.  The server still enforces skill-developer preview access."),
  command("update_agent_skill", "Update Agent Skill", "agentSkillUpdate", { id: id(), input: input("AgentSkillUpdateInput") }, "success agentSkill { id title }", "Update a Linear agent skill.  The server still enforces skill-developer preview access."),
  command("delete_agent_skill", "Delete Agent Skill", "agentSkillDelete", { id: id() }, "success", "Delete a Linear agent skill.  The server still enforces skill-developer preview access.", true),
]);
