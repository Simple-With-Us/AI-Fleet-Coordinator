# Named Linear MCP Commands

AFC-318 adds 175 named commands to the hosted Agent Sync MCP, alongside the existing issue convenience commands and general GraphQL tools.  The catalog is checked against Linear’s official GraphQL schema; availability still depends on the seat app’s permissions and workspace plan.

## Use

- List commands expose the public resource’s filter and pagination arguments, with first defaulting to 20 and capped at 100 when supported.
- Get commands accept the resource ID.  Read results and write receipts are scrubbed, bounded and fenced as untrusted data.
- Write commands require an explicit idempotency_key.  The same key must be reused for retries of the same intended change; uncertain outcomes are never automatically replayed.
- Save commands create when id is omitted and update when id is supplied.  The input object follows the corresponding create/update type in linear_schema.
- Optional selection chooses GraphQL response fields without allowing extra operations.  Use linear_schema to inspect available fields and input types.  Selection syntax is checked locally; Linear validates whether selected fields exist on the response type and returns schema errors for invalid fields.
- Existing app identity checks, tool scopes, budgets, write switch and fleet ownership-label guards apply to every wrapper.

## Uploads And Availability

linear_prepare_attachment_upload prepares private, short-lived signed upload metadata.  It does not upload file bytes or make assets public.  Use the returned method-specific headers and URL only with a trusted upload client, then create the attachment record using the resulting asset URL.  Keep signed URLs/headers out of logs and public posts; use a fresh preparation request after expiry.  The create_attachment_from_upload alias creates the record and assumes the bytes have already been uploaded.

Releases and agent skills are present in the public schema but may depend on workspace plan or preview availability.  Internal diff/review APIs are excluded.  Documentation search and image extraction are not public GraphQL roots and are not advertised as working API commands.  Deprecated cycle creation is excluded because Linear says it is unsupported.  Workspace credential/access administration, batch roots and internal-only arguments and typed input fields remain outside this catalog.

## Catalog

| Command | Access | GraphQL Root | Description |
|---|---|---|---|
| `linear_list_issues` | Read | `issues` | List issues with status, ownership, and project context. |
| `linear_get_issue` | Read | `issue` | Retrieve one issue by ID or identifier. |
| `linear_list_comments` | Read | `comments` | List comments, optionally filtered by issue or another supported comment field. |
| `linear_get_comment` | Read | `comment` | Retrieve one comment by ID. |
| `linear_list_attachments` | Read | `attachments` | List attachments and their linked issues. |
| `linear_get_attachment` | Read | `attachment` | Retrieve one attachment by ID. |
| `linear_list_cycles` | Read | `cycles` | List cycles with their team and date range. |
| `linear_get_cycle` | Read | `cycle` | Retrieve one cycle by ID. |
| `linear_list_custom_views` | Read | `customViews` | List custom views available to the authenticated Linear account. |
| `linear_get_custom_view` | Read | `customView` | Retrieve one custom view by ID. |
| `linear_list_documents` | Read | `documents` | List documents with title, summary, URL, and parent context. |
| `linear_get_document` | Read | `document` | Retrieve one document by ID, including its content. |
| `linear_list_workflow_states` | Read | `workflowStates` | List issue workflow states and their teams. |
| `linear_get_workflow_state` | Read | `workflowState` | Retrieve one workflow state by ID. |
| `linear_get_triage_responsibility` | Read | `triageResponsibility` | Retrieve one triage responsibility by ID. |
| `linear_list_issue_labels` | Read | `issueLabels` | List issue labels with their team and group metadata. |
| `linear_get_issue_label` | Read | `issueLabel` | Retrieve one issue label by ID. |
| `linear_list_project_labels` | Read | `projectLabels` | List project labels with their team and group metadata. |
| `linear_get_project_label` | Read | `projectLabel` | Retrieve one project label by ID. |
| `linear_list_projects` | Read | `projects` | List projects with status, health, and schedule details. |
| `linear_get_project` | Read | `project` | Retrieve one project by ID. |
| `linear_list_project_milestones` | Read | `projectMilestones` | List project milestones and their target dates. |
| `linear_get_project_milestone` | Read | `projectMilestone` | Retrieve one project milestone by ID. |
| `linear_list_teams` | Read | `teams` | List teams visible to the authenticated Linear account. |
| `linear_get_team` | Read | `team` | Retrieve one team by ID or key. |
| `linear_list_users` | Read | `users` | List users visible to the authenticated Linear account. |
| `linear_get_user` | Read | `user` | Retrieve one user by ID. |
| `linear_get_workspace` | Read | `organization` | Retrieve the authenticated workspace's public name and identifier. |
| `linear_list_templates` | Read | `templates` | List templates available to the authenticated Linear account. |
| `linear_search_templates` | Read | `templateSearch` | Find templates matching a Linear template filter. |
| `linear_get_template` | Read | `template` | Retrieve one template by ID, including its template data. |
| `linear_list_notifications` | Read | `notifications` | List notifications visible to the authenticated user. |
| `linear_get_notification` | Read | `notification` | Retrieve one notification by ID. |
| `linear_list_project_updates` | Read | `projectUpdates` | List project updates with health and linked project. |
| `linear_get_project_update` | Read | `projectUpdate` | Retrieve one project update by ID. |
| `linear_list_initiatives` | Read | `initiatives` | List initiatives with owner, health, and target date. |
| `linear_get_initiative` | Read | `initiative` | Retrieve one initiative by ID. |
| `linear_list_initiative_updates` | Read | `initiativeUpdates` | List initiative updates with health and parent initiative. |
| `linear_get_initiative_update` | Read | `initiativeUpdate` | Retrieve one initiative update by ID. |
| `linear_list_favorites` | Read | `favorites` | List favorites for the authenticated user. |
| `linear_get_favorite` | Read | `favorite` | Retrieve one favorite by ID. |
| `linear_list_roadmaps` | Read | `roadmaps` | List roadmaps with owner and workspace details. |
| `linear_get_roadmap` | Read | `roadmap` | Retrieve one roadmap by ID. |
| `linear_list_customer_needs` | Read | `customerNeeds` | List customer needs (Linear's customer request records) with linked customer and issue. |
| `linear_get_customer_need` | Read | `customerNeed` | Retrieve one customer need by ID. |
| `linear_list_customers` | Read | `customers` | List customer records with their key contact and status metadata. |
| `linear_get_customer` | Read | `customer` | Retrieve one customer by ID. |
| `linear_list_releases` | Read | `releases` | List releases with their pipeline and stage.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_get_release` | Read | `release` | Retrieve one release by ID or slug.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_list_release_notes` | Read | `releaseNotes` | List release notes with linked releases.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_get_release_note` | Read | `releaseNote` | Retrieve one release note by ID or slug.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_list_release_pipelines` | Read | `releasePipelines` | List release pipelines and their release settings.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_get_release_pipeline` | Read | `releasePipeline` | Retrieve one release pipeline by ID or slug.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_list_release_stages` | Read | `releaseStages` | List release stages and their parent pipelines.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_get_release_stage` | Read | `releaseStage` | Retrieve one release stage by ID.  Requires a Linear plan or workspace configuration that enables Releases. |
| `linear_list_agent_skills` | Read | `agentSkills` | List agent skills visible to the authenticated Linear account.  Agent skills are a Developer Preview feature. |
| `linear_get_agent_skill` | Read | `agentSkill` | Retrieve one agent skill, including its body.  Agent skills are a Developer Preview feature. |
| `linear_list_issue_statuses` | Read | `workflowStates` | List issue workflow states and their teams.  Alias of linear_list_workflow_states. |
| `linear_get_issue_status` | Read | `workflowState` | Retrieve one workflow state by ID.  Alias of linear_get_workflow_state. |
| `linear_list_milestones` | Read | `projectMilestones` | List project milestones and their target dates.  Alias of linear_list_project_milestones. |
| `linear_get_milestone` | Read | `projectMilestone` | Retrieve one project milestone by ID.  Alias of linear_get_project_milestone. |
| `linear_get_notifications` | Read | `notifications` | List notifications visible to the authenticated user.  Alias of linear_list_notifications. |
| `linear_get_status_updates` | Read | `projectUpdates` | List project updates with health and linked project.  Alias of linear_list_project_updates. |
| `linear_create_issue` | Write | `issueCreate` | Create a Linear issue. |
| `linear_update_issue` | Write | `issueUpdate` | Update a Linear issue. |
| `linear_delete_issue` | Write | `issueDelete` | Delete a Linear issue using Linear's normal deletion policy. |
| `linear_archive_issue` | Write | `issueArchive` | Archive a Linear issue.  Set trash only when moving it to trash is intended. |
| `linear_unarchive_issue` | Write | `issueUnarchive` | Restore an archived Linear issue. |
| `linear_share_issue` | Write | `issueShare` | Share a Linear issue with a user.  Linear enforces record-level access permissions. |
| `linear_unshare_issue` | Write | `issueUnshare` | Remove a user's share access to a Linear issue.  Linear enforces record-level access permissions. |
| `linear_create_comment` | Write | `commentCreate` | Create a comment on a Linear issue. |
| `linear_update_comment` | Write | `commentUpdate` | Update a Linear comment. |
| `linear_delete_comment` | Write | `commentDelete` | Delete a Linear comment. |
| `linear_resolve_comment` | Write | `commentResolve` | Resolve a Linear comment thread. |
| `linear_unresolve_comment` | Write | `commentUnresolve` | Reopen a resolved Linear comment thread. |
| `linear_create_document` | Write | `documentCreate` | Create a Linear document. |
| `linear_update_document` | Write | `documentUpdate` | Update a Linear document. |
| `linear_delete_document` | Write | `documentDelete` | Delete a Linear document. |
| `linear_unarchive_document` | Write | `documentUnarchive` | Restore an archived Linear document. |
| `linear_create_project` | Write | `projectCreate` | Create a Linear project. |
| `linear_update_project` | Write | `projectUpdate` | Update a Linear project. |
| `linear_delete_project` | Write | `projectDelete` | Delete a Linear project. |
| `linear_archive_project` | Write | `projectArchive` | Archive a Linear project.  Set trash only when moving it to trash is intended. |
| `linear_unarchive_project` | Write | `projectUnarchive` | Restore an archived Linear project. |
| `linear_create_issue_label` | Write | `issueLabelCreate` | Create a Linear issue label. |
| `linear_update_issue_label` | Write | `issueLabelUpdate` | Update a Linear issue label. |
| `linear_delete_issue_label` | Write | `issueLabelDelete` | Delete a Linear issue label. |
| `linear_retire_issue_label` | Write | `issueLabelRetire` | Retire a Linear issue label. |
| `linear_restore_issue_label` | Write | `issueLabelRestore` | Restore a retired Linear issue label. |
| `linear_create_project_label` | Write | `projectLabelCreate` | Create a Linear project label. |
| `linear_update_project_label` | Write | `projectLabelUpdate` | Update a Linear project label. |
| `linear_delete_project_label` | Write | `projectLabelDelete` | Delete a Linear project label. |
| `linear_retire_project_label` | Write | `projectLabelRetire` | Retire a Linear project label. |
| `linear_restore_project_label` | Write | `projectLabelRestore` | Restore a retired Linear project label. |
| `linear_create_project_milestone` | Write | `projectMilestoneCreate` | Create a milestone for a Linear project. |
| `linear_update_project_milestone` | Write | `projectMilestoneUpdate` | Update a Linear project milestone. |
| `linear_delete_project_milestone` | Write | `projectMilestoneDelete` | Delete a Linear project milestone. |
| `linear_update_cycle` | Write | `cycleUpdate` | Update a Linear cycle. |
| `linear_archive_cycle` | Write | `cycleArchive` | Archive a Linear cycle. |
| `linear_create_attachment` | Write | `attachmentCreate` | Create an attachment record on a Linear issue.  This does not upload file bytes. |
| `linear_update_attachment` | Write | `attachmentUpdate` | Update a Linear attachment record. |
| `linear_delete_attachment` | Write | `attachmentDelete` | Delete a Linear attachment record. |
| `linear_link_attachment_url` | Write | `attachmentLinkURL` | Link a URL as an attachment to a Linear issue. |
| `linear_prepare_attachment_upload` | Write | `fileUpload` | Prepare signed upload metadata only.  Send file bytes with PUT through the trusted client, and keep the signed URL and headers private; this command does not upload bytes or make the asset public. |
| `linear_create_project_update` | Write | `projectUpdateCreate` | Create a Linear project update. |
| `linear_update_project_update` | Write | `projectUpdateUpdate` | Update a Linear project update. |
| `linear_delete_project_update` | Write | `projectUpdateDelete` | Delete a Linear project update. |
| `linear_archive_project_update` | Write | `projectUpdateArchive` | Archive a Linear project update. |
| `linear_unarchive_project_update` | Write | `projectUpdateUnarchive` | Restore an archived Linear project update. |
| `linear_create_custom_view` | Write | `customViewCreate` | Create a Linear custom view. |
| `linear_update_custom_view` | Write | `customViewUpdate` | Update a Linear custom view. |
| `linear_delete_custom_view` | Write | `customViewDelete` | Delete a Linear custom view. |
| `linear_create_template` | Write | `templateCreate` | Create a Linear template. |
| `linear_update_template` | Write | `templateUpdate` | Update a Linear template. |
| `linear_delete_template` | Write | `templateDelete` | Delete a Linear template. |
| `linear_update_notification` | Write | `notificationUpdate` | Update a Linear notification. |
| `linear_archive_notification` | Write | `notificationArchive` | Archive a Linear notification. |
| `linear_unarchive_notification` | Write | `notificationUnarchive` | Restore an archived Linear notification. |
| `linear_create_issue_relation` | Write | `issueRelationCreate` | Create a relation between Linear issues. |
| `linear_update_issue_relation` | Write | `issueRelationUpdate` | Update a Linear issue relation. |
| `linear_delete_issue_relation` | Write | `issueRelationDelete` | Delete a Linear issue relation. |
| `linear_create_initiative` | Write | `initiativeCreate` | Create a Linear initiative. |
| `linear_update_initiative` | Write | `initiativeUpdate` | Update a Linear initiative. |
| `linear_delete_initiative` | Write | `initiativeDelete` | Delete a Linear initiative. |
| `linear_archive_initiative` | Write | `initiativeArchive` | Archive a Linear initiative. |
| `linear_unarchive_initiative` | Write | `initiativeUnarchive` | Restore an archived Linear initiative. |
| `linear_create_initiative_update` | Write | `initiativeUpdateCreate` | Create a Linear initiative update. |
| `linear_update_initiative_update` | Write | `initiativeUpdateUpdate` | Update a Linear initiative update. |
| `linear_archive_initiative_update` | Write | `initiativeUpdateArchive` | Archive a Linear initiative update. |
| `linear_unarchive_initiative_update` | Write | `initiativeUpdateUnarchive` | Restore an archived Linear initiative update. |
| `linear_create_initiative_relation` | Write | `initiativeRelationCreate` | Create a parent-child relation between Linear initiatives. |
| `linear_update_initiative_relation` | Write | `initiativeRelationUpdate` | Update a Linear initiative relation. |
| `linear_delete_initiative_relation` | Write | `initiativeRelationDelete` | Delete a Linear initiative relation. |
| `linear_add_initiative_project` | Write | `initiativeToProjectCreate` | Associate a Linear project with an initiative. |
| `linear_update_initiative_project` | Write | `initiativeToProjectUpdate` | Update a Linear initiative-to-project association. |
| `linear_remove_initiative_project` | Write | `initiativeToProjectDelete` | Remove a project from a Linear initiative. |
| `linear_create_workflow_state` | Write | `workflowStateCreate` | Create a Linear workflow state. |
| `linear_update_workflow_state` | Write | `workflowStateUpdate` | Update a Linear workflow state. |
| `linear_archive_workflow_state` | Write | `workflowStateArchive` | Archive a Linear workflow state.  Linear requires its issues to be archived first. |
| `linear_complete_release` | Write | `releaseComplete` | Mark a Linear release as completed.  The server still enforces release-plan permissions. |
| `linear_create_release` | Write | `releaseCreate` | Create a Linear release.  The server still enforces release-plan permissions. |
| `linear_update_release` | Write | `releaseUpdate` | Update a Linear release.  The server still enforces release-plan permissions. |
| `linear_delete_release` | Write | `releaseDelete` | Delete a Linear release.  The server still enforces release-plan permissions. |
| `linear_archive_release` | Write | `releaseArchive` | Archive a Linear release.  The server still enforces release-plan permissions. |
| `linear_unarchive_release` | Write | `releaseUnarchive` | Restore an archived Linear release.  The server still enforces release-plan permissions. |
| `linear_create_release_note` | Write | `releaseNoteCreate` | Create a Linear release note.  The server still enforces release-plan permissions. |
| `linear_update_release_note` | Write | `releaseNoteUpdate` | Update a Linear release note.  The server still enforces release-plan permissions. |
| `linear_delete_release_note` | Write | `releaseNoteDelete` | Delete a Linear release note.  The server still enforces release-plan permissions. |
| `linear_create_agent_skill` | Write | `agentSkillCreate` | Create a Linear agent skill.  The server still enforces skill-developer preview access. |
| `linear_update_agent_skill` | Write | `agentSkillUpdate` | Update a Linear agent skill.  The server still enforces skill-developer preview access. |
| `linear_delete_agent_skill` | Write | `agentSkillDelete` | Delete a Linear agent skill.  The server still enforces skill-developer preview access. |
| `linear_save_issue` | Write | `issueCreate` | Create or update a issue.  Supply id to update; omit id to create. |
| `linear_save_comment` | Write | `commentCreate` | Create or update a comment.  Supply id to update; omit id to create. |
| `linear_save_document` | Write | `documentCreate` | Create or update a document.  Supply id to update; omit id to create. |
| `linear_save_project` | Write | `projectCreate` | Create or update a project.  Supply id to update; omit id to create. |
| `linear_save_issue_label` | Write | `issueLabelCreate` | Create or update a issue label.  Supply id to update; omit id to create. |
| `linear_save_project_label` | Write | `projectLabelCreate` | Create or update a project label.  Supply id to update; omit id to create. |
| `linear_save_project_milestone` | Write | `projectMilestoneCreate` | Create or update a project milestone.  Supply id to update; omit id to create. |
| `linear_save_attachment` | Write | `attachmentCreate` | Create or update a attachment.  Supply id to update; omit id to create. |
| `linear_save_project_update` | Write | `projectUpdateCreate` | Create or update a project update.  Supply id to update; omit id to create. |
| `linear_save_custom_view` | Write | `customViewCreate` | Create or update a custom view.  Supply id to update; omit id to create. |
| `linear_save_template` | Write | `templateCreate` | Create or update a template.  Supply id to update; omit id to create. |
| `linear_save_issue_relation` | Write | `issueRelationCreate` | Create or update a issue relation.  Supply id to update; omit id to create. |
| `linear_save_initiative` | Write | `initiativeCreate` | Create or update a initiative.  Supply id to update; omit id to create. |
| `linear_save_initiative_update` | Write | `initiativeUpdateCreate` | Create or update a initiative update.  Supply id to update; omit id to create. |
| `linear_save_initiative_relation` | Write | `initiativeRelationCreate` | Create or update a initiative relation.  Supply id to update; omit id to create. |
| `linear_save_workflow_state` | Write | `workflowStateCreate` | Create or update a workflow state.  Supply id to update; omit id to create. |
| `linear_save_release` | Write | `releaseCreate` | Create or update a release.  Supply id to update; omit id to create. |
| `linear_save_release_note` | Write | `releaseNoteCreate` | Create or update a release note.  Supply id to update; omit id to create. |
| `linear_save_agent_skill` | Write | `agentSkillCreate` | Create or update a agent skill.  Supply id to update; omit id to create. |
| `linear_save_milestone` | Write | `projectMilestoneCreate` | Create or update a project milestone.  Supply id to update; omit id to create.  Alias of linear_save_project_milestone. |
| `linear_save_status_update` | Write | `projectUpdateCreate` | Create or update a project update.  Supply id to update; omit id to create.  Alias of linear_save_project_update. |
| `linear_delete_status_update` | Write | `projectUpdateDelete` | Delete a Linear project update.  Alias of linear_delete_project_update. |
| `linear_mark_notification` | Write | `notificationUpdate` | Update a Linear notification.  Alias of linear_update_notification. |
| `linear_create_attachment_from_upload` | Write | `attachmentCreate` | Create an attachment record for an already uploaded asset.  Set input.url to the assetUrl from linear_prepare_attachment_upload and input.issueId to the target issue.  This does not upload file bytes. |

## Sources

- [Official GraphQL schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql)
- [GraphQL API](https://linear.app/developers/graphql)
- [File upload flow](https://linear.app/developers/how-to-upload-a-file-to-linear)
