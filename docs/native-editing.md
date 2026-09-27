# Native patch syntax, observations, and limits

This adapter preserves Codex's native patch tool. It does not implement its own file editor. The syntax and filesystem behavior described here belong to the tested Codex version.

## Basic syntax

```text
*** Begin Patch
*** Add File: notes.txt
+A new file.
*** Update File: calculator.py
@@
-    return a - b
+    return a + b
*** Delete File: obsolete.txt
*** End Patch
```

Each line added to a new file begins with `+`. An update uses unchanged context lines prefixed with a space, removed lines prefixed with `-`, and added lines prefixed with `+`.

A hunk header can be bare `@@` **or `@@ ` followed by a text anchor**, for example `@@ def add(a, b):`. Numeric Git headers such as `@@ -1,3 +1,3 @@` are not parsed as line ranges: the text is treated as an anchor, which normally fails to match. The [pinned parser grammar and tests](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/apply-patch/src/parser.rs) document both accepted header forms.

For a rename, use `*** Update File: old/path` followed by `*** Move to: new/path` and at least one hunk. A context-only hunk can express a rename without changing text.

## User-supplied manual validation report

The maintainer supplied a report on 2026-09-27 describing 17 operation probes and nine failure-mode categories. The table below records the enumerated operations and additional observations from that report. Raw private session logs and project files are not published. These are **reported observations**, separate from this repository's automated bridge tests and release smoke test.

| Operation | Reported result |
| --- | --- |
| Add a flat-path file | Passed |
| Add under nested missing directories | Passed; parents created |
| Add with no content lines | Passed; zero-byte file |
| Update a line | Passed |
| Two update hunks in one file | Passed |
| Pure insertion | Passed |
| Pure deletion through EOF | Passed |
| Insertion with no old context | Appended at EOF |
| Rename with edits | Passed |
| Rename into a missing directory | Passed; directory created |
| Delete a file | Passed |
| One patch mixing add/update/move/delete | Passed |
| Relative and absolute paths | Passed under the session's execution permissions |
| Paths containing spaces | Passed |
| Leading marker characters, UTF-8, and tabs | Preserved after the patch's leading content marker |
| Generated Rust file | Compiled and ran in the reported check |

Absolute paths outside the workspace being usable in an unrestricted session do not imply that sandboxed sessions allow them. Permission behavior depends on Codex's execution policy and host.

## Reported filesystem gotchas

- Updating a CRLF file normalized its line endings to LF.
- Updating a file without a final newline added a trailing newline.
- `Add File` overwrote an existing file; it was not a create-exclusive operation.
- Moving onto an existing destination replaced its contents and removed the source.
- Move summaries used `M` for the destination, not a separate `R` marker.
- Deleting or moving files left empty parent directories behind.

Use version control and review diffs when line endings or existing destinations matter. “Content preserved” in the Unicode probe is not a guarantee of byte-identical line-ending preservation.

## Failure probes and the transaction boundary

The reported failures covered missing begin/end markers, an empty patch, an unknown operation, missing update/delete targets, unmatched context, a directory deletion, and two mixed patches containing an otherwise valid add followed by a failing operation. The two mixed patches failed before the new files were written.

Those results demonstrate pre-application validation for those cases. They do **not** establish a general all-or-nothing filesystem transaction. The [pinned application implementation](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/apply-patch/src/lib.rs) tracks committed changes and explicitly accounts for writes that modify a target before failing. Its tests also cover a destination write succeeding before source removal fails during a move. Disk-full errors, permissions, process interruption, and concurrent changes can therefore have different outcomes from a validation error.

The bridge adds no rollback layer. Inspect actual files and diffs after a failed application.

## Reproducing this project's integration check

```bash
python3 scripts/smoke_test.py --launcher codex-local
```

This automated live check proves native add/update/delete events, correct resulting files, and unchanged passing tests. It does not claim to reproduce every manual probe above, compile Rust, or verify all filesystem failure modes.
