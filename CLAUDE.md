# Delegation guidance for qwen-local-delegate-mcp

> **This file exists as a copy-paste source.** After installing `qwen-local-delegate-mcp`,
> paste the block below into **your own** `CLAUDE.md`, either at user scope
> (`~/.claude/CLAUDE.md`, applies to every project) or in a specific project's
> root `CLAUDE.md`. Without it, Claude will not reliably use the delegation
> tools; a bare "delegate to qwen" is ambiguous and Claude may look for a
> nonexistent `qwen` CLI instead.

---

## Delegation to local Qwen

You have access to a local Qwen model (via the `qwen-local-delegate` MCP server) that
runs free and offline. It is a cheap executor, not a cheap thinker.

**Decide by verification cost, not by task size.** Delegating optimizes the
abundant resource (writing code) and taxes the scarce one (verifying it). If
checking Qwen's output would cost about what writing it yourself costs, write it
yourself. Delegation pays when a cheap mechanical verifier already exists: a
test suite, a type checker, a diff you can skim.

**Never delegate work whose failure mode is silent.** A textbook-looking formula
that passes toy unit tests and is quietly wrong on real data is exactly what a
cheap model writes and a cheap verifier green-lights. If a bug here would be
subtle, systemic, and invisible to the obvious test, write it yourself.

**Delegation is not a goal in itself.** Many sessions contain almost no bulk
code-writing, and forcing delegation into one makes the work worse rather than
cheaper. Routing nothing to Qwen in a session is a perfectly good outcome.

### Good candidates

Many near-identical mechanical units with an oracle already in place: bulk
renames, docstring passes, boilerplate, test scaffolding from a fixed template,
format conversions, mechanical translations, first drafts of prose. One good
brief, N outputs, `npm test` or equivalent to check them.

### Do it yourself

- Architectural decisions, ambiguous debugging, cross-cutting design, code
  review, anything requiring judgment about the codebase's intent.
- Anything whose failure would be silent (see above).
- Anything needing images. This path is text only.
- Anything you could do in one thought. The round trip is not worth it.
- Anything where writing the brief costs more than writing the code.
- Anything needing context you have but Qwen does not, unless it is small enough
  to paste in.
- The first exploration of an unfamiliar codebase. Build the mental model
  yourself.

### How to delegate

1. **Open ONE session per coherent task** with `qwen_start_session`. Pass a
   short `topic` so it is easy to find later, and a `system_prompt` that gives
   Qwen its role and constraints.

2. **Reuse the same `session_id`** across all follow-ups for that task via
   `qwen_send(session_id, ...)`. Do NOT start a fresh session per file or per
   iteration; that throws away Qwen's memory of what it just did.

3. **Pass files via `qwen_send`'s `files` argument, not by reading them
   yourself first.** `qwen_send(session_id, message, files=["/abs/path.py"])`
   has the server read the bytes straight off disk into Qwen's prompt. Reading
   the file into your own context first and pasting it into `message` defeats
   the point of delegating — you pay for the bulk twice.

4. **Verify Qwen's output before applying it.** Qwen is smaller and cheaper than
   you and can hallucinate. Treat its replies as drafts: when it emits code,
   read it; when it edits files, diff-review it.

5. **Assume Qwen can crash mid-task.** It has before. Keep delegated work
   resumable, and do not stack a long unrecoverable chain behind it.

6. **A `qwen_send` timeout is NOT a Qwen failure.** The MCP call can idle out
   (~300s) while Ollama keeps generating; the finished reply still lands in the
   session transcript. After any timeout, call `qwen_get_history` FIRST and only
   re-ask if it confirms nothing was produced. Re-asking blind double-generates
   and desyncs the session — the model answers twice and the transcript no
   longer matches what you think you sent. To avoid the timeout, size each
   turn's output to ≲150 lines, or raise the per-server MCP `timeout`.

7. **Read `qwen_get_history` before `qwen_end_session`.** Closing deletes the
   transcript, and that transcript is the only evidence of whether delegation
   worked. Skim it, then close.

8. **When resuming work in a fresh conversation**, call `qwen_list_sessions`
   first to check for an existing session for this task. Resume it instead of
   starting a duplicate.

### Example prompt shapes to Qwen

Good:
- "Rewrite this function to use async/await. Preserve behavior. Return only the
  new function body." (concrete, verifiable)
- "Generate Google-style docstrings for these five functions. Return each as
  `def name(...): \"\"\"...\"\"\"`." (mechanical, structured output)

Bad (do these yourself):
- "Is this the right architecture?" (judgment call)
- "Debug why the tests are flaky." (needs to run code, form hypotheses)

Whatever the shape, state the codebase's style conventions in the brief (e.g.
"semicolon-free, prefer `const`"). Qwen's logic is usually sound; its deviations
tend to be cosmetic, and a one-line convention note heads them off before you
have to fix them by hand.
