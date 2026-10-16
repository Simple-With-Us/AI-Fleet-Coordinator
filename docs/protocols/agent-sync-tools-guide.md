# Agent-Sync Tools Guide

The one guide to the Agent-Sync tools on every transport:  the stdio server (`agent-sync mcp`), the hosted server (`agent-sync.jays.services`) and the `agent-sync` CLI.  It is served three ways:  the `guide` tool (whole, or one `##` section by name), `https://fleetlink.online/agent-sync-tools-guide/` and the `agent-sync-tools-guide` skill.  Design and rules:  `docs/protocols/per-call-identity.md`.

This page is a skeleton.  Each section below is being written by a later change (AFC-331);  until then the `guide` tool returns these headings so a client can see what is coming.

## How to Pass Identity

Being written:  the `identity` argument, the write rule (`identity_required`), the default identity for reads and the allowed sets.

## Which Connection a Client Gets

Being written:  stdio for a Mac app that can run a command, hosted for a client that only speaks remote MCP, and how the owner arms a hosted seat.

## Tools by Family

Being written:  every tool with a one-line purpose, grouped as `zulip_*`, `listener_*`, `linear_*`, `recall_*`, `notes_*`, `fleetlink_*` and the unprefixed `whoami`, `identities` and `guide`.

## Old Names and New Names

Being written:  the table of removed tool names and what replaced each (`renamed-tools.json`).

## The Notes Relay

Being written:  how `notes_*` writes directly on a Mac and through `#notes-relay` everywhere else, the title rule `[APP, Agent] topic`, `notes_status`, and what the owner sets up.

## CLI Equivalents

Being written:  the `agent-sync zulip`, `listener` and `notes` command groups and the flat shortcuts, tool by tool.
