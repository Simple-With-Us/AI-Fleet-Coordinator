// Static schemas captured from Linear's official MCP tools/list response.
const OFFICIAL_TOOLS = [
  {
    "name": "get_diff",
    "title": "Get diff",
    "description": "Exact lookup for a Linear diff. Use with review URLs, GitHub PR URLs, Linear full identifiers, UUIDs, or slugs.",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "urlOrId": {
          "type": "string",
          "minLength": 1,
          "description": "Linear review URL, diff slug, pull request ID, Linear full identifier, or GitHub PR URL"
        }
      },
      "required": [
        "urlOrId"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": true,
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false
    }
  },
  {
    "name": "list_diffs",
    "title": "List diffs",
    "description": "List Linear diff pull requests visible to the authenticated user",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "limit": {
          "default": 50,
          "description": "Maximum number of results to return per page",
          "type": "number",
          "maximum": 250
        },
        "cursor": {
          "description": "Omit for the first page. To continue, pass the previous response's cursor when hasNextPage is true",
          "type": "string"
        },
        "orderBy": {
          "default": "updatedAt",
          "description": "Timestamp to sort results by: creation time or last update time",
          "type": "string",
          "enum": [
            "createdAt",
            "updatedAt"
          ]
        },
        "query": {
          "description": "Broad search by title, branch, PR number, or bare slug",
          "type": "string"
        },
        "owner": {
          "description": "Filter returned diffs by repository owner",
          "type": "string"
        },
        "repo": {
          "description": "Filter returned diffs by repository name",
          "type": "string"
        },
        "status": {
          "description": "Filter returned diffs by pull request status",
          "type": "string"
        },
        "author": {
          "description": "Filter by author ID, name, email, or \"me\"",
          "type": "string"
        },
        "reviewer": {
          "description": "Filter by reviewer ID, name, email, or \"me\"",
          "type": "string"
        },
        "reviewState": {
          "description": "Reviewer state; requires reviewer and defaults to pending when reviewer is set",
          "type": "string",
          "enum": [
            "pending",
            "approved",
            "changesRequested",
            "commented",
            "dismissed"
          ]
        }
      },
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": true,
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false
    }
  },
  {
    "name": "get_diff_threads",
    "title": "Get diff threads",
    "description": "Exact lookup for diff threads and the authenticated user's drafts. Use with review URLs, GitHub PR URLs, Linear full identifiers, UUIDs, or slugs.",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "urlOrId": {
          "type": "string",
          "minLength": 1,
          "description": "Linear review URL, diff slug, pull request ID, Linear full identifier, or GitHub PR URL"
        },
        "threadId": {
          "description": "Optional Linear comment UUID of any comment in the thread (not its `externalThreadId`); a reply's UUID resolves to its thread root",
          "type": "string"
        },
        "resolved": {
          "description": "Filter returned threads by resolved state",
          "type": "boolean"
        },
        "orderBy": {
          "default": "updatedAt",
          "description": "Sort: createdAt | updatedAt",
          "type": "string",
          "enum": [
            "createdAt",
            "updatedAt"
          ]
        }
      },
      "required": [
        "urlOrId"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": true,
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false
    }
  },
  {
    "name": "save_diff_comment",
    "title": "Save diff comment",
    "description": "Create, reply to, or edit a comment or persisted draft on a Linear diff. Set draft to true to save without submitting. Provide draftId to edit or submit a persisted draft, and commentId only to edit a submitted comment. Submitting an inline draft reuses its saved anchor; submitting a reply draft requires parentId again.",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "urlOrId": {
          "description": "Linear review URL, diff slug, pull request ID, Linear full identifier, or GitHub PR URL",
          "type": "string",
          "minLength": 1
        },
        "commentId": {
          "description": "Existing comment ID to edit",
          "type": "string"
        },
        "draftId": {
          "description": "Existing persisted draft ID to edit or submit",
          "type": "string"
        },
        "draft": {
          "default": false,
          "description": "Save as a private persisted draft without submitting",
          "type": "boolean"
        },
        "parentId": {
          "description": "Top-level comment ID to reply to; also required when submitting a persisted reply draft",
          "type": "string"
        },
        "body": {
          "type": "string",
          "minLength": 1,
          "description": "Content as Markdown. Do not escape the string — use literal newlines and special characters, not escape sequences"
        },
        "anchor": {
          "description": "Optional comment anchor copied from a diff thread or built for a file or diff hunk",
          "type": "object",
          "propertyNames": {
            "type": "string"
          },
          "additionalProperties": {}
        },
        "anchorContent": {
          "description": "Exact source text of the anchor's end line; stored with an inline draft so it can be re-anchored after the diff receives new revisions",
          "type": "string"
        }
      },
      "required": [
        "body"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": false,
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true
    }
  },
  {
    "name": "resolve_diff_thread",
    "title": "Resolve diff thread",
    "description": "Resolve or reopen a top-level comment thread on a Linear diff",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "threadId": {
          "type": "string",
          "minLength": 1,
          "description": "Top-level diff thread/comment ID"
        },
        "resolved": {
          "default": true,
          "description": "True to resolve the thread; false to reopen it",
          "type": "boolean"
        }
      },
      "required": [
        "threadId"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": false,
      "destructiveHint": true,
      "idempotentHint": true,
      "openWorldHint": true
    }
  },
  {
    "name": "delete_diff_comment",
    "title": "Delete diff comment",
    "description": "Delete a comment or persisted draft from a Linear diff",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "commentId": {
          "description": "Submitted diff comment ID to delete",
          "type": "string",
          "minLength": 1
        },
        "draftId": {
          "description": "Persisted diff comment draft ID to delete",
          "type": "string",
          "minLength": 1
        }
      },
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": false,
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true
    }
  },
  {
    "name": "submit_diff_review",
    "title": "Submit diff review",
    "description": "Approve a Linear diff, request changes, or submit a review comment",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "urlOrId": {
          "type": "string",
          "minLength": 1,
          "description": "Linear review URL, diff slug, pull request ID, Linear full identifier, or GitHub PR URL"
        },
        "decision": {
          "type": "string",
          "enum": [
            "approved",
            "changesRequested",
            "commented"
          ],
          "description": "Review decision: approved, changesRequested, or commented"
        },
        "body": {
          "description": "Content as Markdown. Do not escape the string — use literal newlines and special characters, not escape sequences. Required when requesting changes or commenting",
          "type": "string",
          "minLength": 1
        }
      },
      "required": [
        "urlOrId",
        "decision"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": false,
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true
    }
  },
  {
    "name": "update_diff",
    "title": "Update diff",
    "description": "Update a Linear diff's title, description, reviewer requests, issue links, or status. Provide at least one update. Combined updates can partially succeed; inspect the result before retrying. Use merge_diff to merge and submit_diff_review to approve or request changes.",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "urlOrId": {
          "type": "string",
          "minLength": 1,
          "description": "Linear review URL, diff slug, pull request ID, Linear full identifier, or GitHub PR URL"
        },
        "title": {
          "description": "New diff title",
          "type": "string",
          "minLength": 1
        },
        "description": {
          "description": "New description as Markdown; an empty string clears it",
          "type": "string"
        },
        "addedReviewRequests": {
          "description": "Request or re-request reviews; existing pending requests are unchanged",
          "minItems": 1,
          "maxItems": 25,
          "type": "array",
          "items": {
            "anyOf": [
              {
                "type": "object",
                "properties": {
                  "user": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Workspace user ID, name, email, or \"me\""
                  }
                },
                "required": [
                  "user"
                ],
                "additionalProperties": false
              },
              {
                "type": "object",
                "properties": {
                  "externalUserId": {
                    "type": "string",
                    "format": "uuid",
                    "pattern": "^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$",
                    "description": "External user ID from get_diff reviewer metadata"
                  }
                },
                "required": [
                  "externalUserId"
                ],
                "additionalProperties": false
              },
              {
                "type": "object",
                "properties": {
                  "githubTeamId": {
                    "type": "string",
                    "format": "uuid",
                    "pattern": "^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$",
                    "description": "GitHub team ID from get_diff reviewer metadata"
                  }
                },
                "required": [
                  "githubTeamId"
                ],
                "additionalProperties": false
              }
            ]
          }
        },
        "removedReviewRequests": {
          "description": "Withdraw pending review requests; submitted reviews are preserved",
          "minItems": 1,
          "maxItems": 25,
          "type": "array",
          "items": {
            "anyOf": [
              {
                "type": "object",
                "properties": {
                  "user": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Workspace user ID, name, email, or \"me\""
                  }
                },
                "required": [
                  "user"
                ],
                "additionalProperties": false
              },
              {
                "type": "object",
                "properties": {
                  "externalUserId": {
                    "type": "string",
                    "format": "uuid",
                    "pattern": "^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$",
                    "description": "External user ID from get_diff reviewer metadata"
                  }
                },
                "required": [
                  "externalUserId"
                ],
                "additionalProperties": false
              },
              {
                "type": "object",
                "properties": {
                  "githubTeamId": {
                    "type": "string",
                    "format": "uuid",
                    "pattern": "^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$",
                    "description": "GitHub team ID from get_diff reviewer metadata"
                  }
                },
                "required": [
                  "githubTeamId"
                ],
                "additionalProperties": false
              }
            ]
          }
        },
        "addedIssueLinks": {
          "description": "Add issue links or change the relationship of an existing link",
          "minItems": 1,
          "maxItems": 25,
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "issue": {
                "type": "string",
                "minLength": 1,
                "description": "Linear issue identifier or UUID"
              },
              "type": {
                "type": "string",
                "enum": [
                  "closes",
                  "contributes",
                  "links",
                  "reopens"
                ],
                "description": "Relationship: closes, contributes, links, or reopens"
              }
            },
            "required": [
              "issue",
              "type"
            ],
            "additionalProperties": false
          }
        },
        "removedIssueLinks": {
          "description": "Linear issue identifiers or UUIDs to unlink",
          "minItems": 1,
          "maxItems": 25,
          "type": "array",
          "items": {
            "type": "string",
            "minLength": 1
          }
        },
        "statusAction": {
          "description": "Status transition to perform after metadata is updated",
          "type": "string",
          "enum": [
            "markReadyForReview",
            "convertToDraft",
            "close",
            "reopen"
          ]
        }
      },
      "required": [
        "urlOrId"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": false,
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true
    }
  },
  {
    "name": "merge_diff",
    "title": "Merge diff",
    "description": "Merge a Linear diff or add it to the repository's merge queue",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "urlOrId": {
          "type": "string",
          "minLength": 1,
          "description": "Linear review URL, diff slug, pull request ID, Linear full identifier, or GitHub PR URL"
        },
        "mergeMethod": {
          "description": "Merge method; omit to use the repository default",
          "type": "string",
          "enum": [
            "MERGE",
            "REBASE",
            "SQUASH"
          ]
        }
      },
      "required": [
        "urlOrId"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": false,
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true
    }
  },
  {
    "name": "extract_images",
    "title": "Extract images",
    "description": "Extract and fetch images from markdown content. Use this to view screenshots, diagrams, or other images embedded in Linear issues, comments, or documents. Pass the markdown content (e.g., issue description) and receive the images as viewable data.",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "markdown": {
          "type": "string",
          "description": "Markdown content containing image references (e.g., issue description, comment body)"
        }
      },
      "required": [
        "markdown"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": true,
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false
    }
  },
  {
    "name": "search_documentation",
    "title": "Search documentation",
    "description": "Search Linear's product documentation for feature and usage guidance. Returns up to five results with titles, URLs, and content snippets per page.",
    "inputSchema": {
      "type": "object",
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "properties": {
        "query": {
          "type": "string",
          "description": "Feature name, topic, or question to search for in Linear's product documentation"
        },
        "page": {
          "default": 0,
          "description": "Zero-based page number. Increment by one to fetch the next page for the same query",
          "type": "number"
        }
      },
      "required": [
        "query"
      ],
      "additionalProperties": false
    },
    "annotations": {
      "readOnlyHint": true,
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false
    }
  }
];

const MAX_STRING_LENGTH = 24000;
const MAX_LIST_ITEMS = 250;
const outputLimit = { type: "integer", minimum: 1000, maximum: 64000, default: 24000 };
const idempotencyKey = { type: "string", minLength: 1, maxLength: 100, pattern: "^[A-Za-z0-9_.:-]+$" };
const session = { type: "string", maxLength: 80, pattern: "^[A-Za-z0-9_.:-]+$" };

function boundedSchema(schema, depth = 0) {
  if (depth > 32 || !schema || typeof schema !== "object" || Array.isArray(schema)) return schema;
  const out = { ...schema };
  if (out.type === "string") {
    out.maxLength = Number.isInteger(out.maxLength) ? Math.min(out.maxLength, MAX_STRING_LENGTH) : MAX_STRING_LENGTH;
  }
  if (out.type === "array") {
    out.maxItems = Number.isInteger(out.maxItems) ? Math.min(out.maxItems, MAX_LIST_ITEMS) : MAX_LIST_ITEMS;
    if (out.items && typeof out.items === "object") out.items = boundedSchema(out.items, depth + 1);
  }
  if (out.type === "object") {
    out.maxProperties = Number.isInteger(out.maxProperties) ? Math.min(out.maxProperties, 100) : 100;
    if (out.properties && typeof out.properties === "object") {
      out.properties = Object.fromEntries(Object.entries(out.properties).map(([key, value]) => [key, boundedSchema(value, depth + 1)]));
    }
    if (out.additionalProperties && typeof out.additionalProperties === "object") {
      out.additionalProperties = boundedSchema(out.additionalProperties, depth + 1);
    }
  }
  if (Array.isArray(out.anyOf)) out.anyOf = out.anyOf.map((item) => boundedSchema(item, depth + 1));
  if (Array.isArray(out.oneOf)) out.oneOf = out.oneOf.map((item) => boundedSchema(item, depth + 1));
  if (Array.isArray(out.allOf)) out.allOf = out.allOf.map((item) => boundedSchema(item, depth + 1));
  return out;
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

const built = OFFICIAL_TOOLS.map((upstream) => {
  const write = upstream.annotations?.readOnlyHint !== true;
  const schema = boundedSchema(upstream.inputSchema);
  const properties = { ...(schema.properties ?? {}), max_chars: outputLimit };
  const required = [...(schema.required ?? [])];
  if (write) {
    properties.idempotency_key = idempotencyKey;
    properties.session = session;
    required.push("idempotency_key");
  }
  const name = `linear_${upstream.name}`;
  const description = `${upstream.description}  Uses this seat's Linear app and existing API budgets.${write ? "  Requires an idempotency key; uncertain outcomes are never automatically replayed." : ""}`;
  return deepFreeze({
    name,
    upstreamName: upstream.name,
    title: `Linear ${upstream.name.split("_").map((word) => word[0].toUpperCase() + word.slice(1)).join(" ")}`,
    description,
    write,
    inputSchema: { ...schema, additionalProperties: false, properties, ...(required.length ? { required: [...new Set(required)] } : {}) },
    annotations: { ...upstream.annotations },
  });
});

export const LINEAR_MCP_TOOLS = Object.freeze(built.map(({ upstreamName, write, ...tool }) => tool));
export const LINEAR_MCP_BY_NAME = Object.freeze(Object.fromEntries(built.map((tool) => [tool.name, tool])));
export const LINEAR_MCP_BY_UPSTREAM_NAME = Object.freeze(Object.fromEntries(built.map((tool) => [tool.upstreamName, tool])));
export const LINEAR_MCP_READ_TOOLS = Object.freeze(new Set(built.filter((tool) => !tool.write).map((tool) => tool.name)));
export const LINEAR_MCP_WRITE_TOOLS = Object.freeze(new Set(built.filter((tool) => tool.write).map((tool) => tool.name)));
export const OFFICIAL_LINEAR_MCP_TOOL_NAMES = Object.freeze(new Set(built.map((tool) => tool.upstreamName)));
