# Required local generation, fail-closed candidate packaging

The published source intentionally omits native_source_schemas.cjs and schema-provenance.json because the normal publication scanner refused these source-derived files. No encoded/disguised copy is published, and no scanner bypass is used. Generate them only from the authorized pinned installed source before tests or qualification:

    python prepare_dependencies.py
    node prepare_candidate.cjs SOURCE

Missing/mismatched generated schema/provenance stops native_flow at module load. A deployment script must not activate or install this candidate merely because preparation succeeded. The pinned SOURCE is required, never fetched from an untrusted URL.

# Reproduce source declarations

Use Node22 and Python3. No package install or network is needed; MIT-licensed Acorn8.15.0 and its license are included under vendor/acorn. Zod and its license are included separately.

Supply a copy of the installed release's server-core.js as SOURCE. It must hash to ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0. The full multi-megabyte bundle is not included because it contains unrelated embedded application prompts and unrelated code. The selected source declarations are included, not hidden behind remote access.

    node verify_provenance.cjs SOURCE

This checks the original source hash, every declaration's exact source-slice hash using recorded UTF16 offsets, the generated file hashes, then regenerates schemas and installed scope/socket functions into a temporary directory and requires byte-identical outputs. Set TMPDIR to a writable filesystem if /tmp is full.

Without SOURCE, `node verify_provenance.cjs` checks generated-file hashes only and explicitly reports that weaker result. It does not independently authenticate source provenance.

Portable extraction entry points:

    node extract_source_schemas.cjs SOURCE OUTPUT_DIRECTORY
    node extract_scope.cjs SOURCE OUTPUT_DIRECTORY
    python extract_assembler.py

The locally generated schema-provenance.json records243 named declaration entries with source offsets and hashes. scope-provenance.json records exact connection-scope/helper/socket function offsets, hashes and generated hashes. Source offsets are JavaScript UTF16 code units, not UTF8 bytes. Extractor imports are limited to bundled Zod for generated schemas. Acorn is used to locate declarations, not execute the full application bundle. The declaration closure is a static extraction, not a vendor-supported schema export.

extract_assembler.py rebuilds native_assembler.cjs from bundled captured source JSON, preserving exact assembler and helper implementations with the stated import removal. Earlier capture JSON records source offsets. Its exact assembler containment was independently checked in review1103.
