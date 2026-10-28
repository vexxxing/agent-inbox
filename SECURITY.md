# Security

## Threat model in one paragraph

agent-inbox carries text from the outside world (webhook senders, web pages, feeds) to an AI agent that can act. The risks are: someone posting events they shouldn't (forged events), sensitive data ending up in comments (leaks), and text inside an event steering the agent (prompt injection). The template defends against the first two with tokens, a shared secret and filters. The third can only be handled by the agent, so every agent that reads the inbox must be told that event content is data, never instructions.

## What the template does

- **Shared secret.** `repository_dispatch` events are rejected unless `client_payload.secret` matches the `INBOX_SECRET` repository secret (constant-time comparison). With no secret set, every dispatch is refused rather than let through.
- **Least privilege.** Each workflow asks only for the permissions it needs. The relay can read code and write comments; only the watchers job can push, and only to its own state branch.
- **No shell injection.** Event data never appears inside `${{ }}` expressions in workflow scripts; it reaches Python through the event file and environment variables. A test enforces this.
- **Nothing echoed.** A rejected request is logged with a reason only, never its contents.
- **Safe rendering.** `type`, `source` and `id` are restricted to a plain alphabet. Inside the JSON block, `` ` `` `<` `>` `@` and `&` are escaped, so payload text can't close the code block, inject HTML or @mention anyone.
- **Filters.** `inbox.yml` limits event types, sources and payload size, and strips sensitive-looking keys (`secret`, `token`, `password`, `authorization`, `api_key`) at any depth.
- **Watchers stay dumb.** They fetch one URL, cap the response at 2 MB, refuse XML with DOCTYPE or ENTITY declarations, and run no AI and no page scripts. `file:` URLs only work with an explicit `--allow-file` flag for local tests. Header values can read environment variables you add for API keys, but never `GITHUB_*`, `ACTIONS_*`, `RUNNER_*` or `INBOX_*`, so the workflow's own tokens can't be sent to a watched site.
- **Pinned dependencies.** `requirements.txt` pins exact versions.

## What you should do

1. **Keep the repository private.**
2. **Use fine-grained tokens scoped to this one repository, with an expiry date.**
   - Dispatch senders: **Contents: Read and write** only. That permission can also push files, so keep nothing else of value in this repository, and protect `main` with a branch ruleset if you can. Don't add the **Workflows** permission.
   - Optional `INBOX_TOKEN` (posting as an account other than `github-actions[bot]`): **Pull requests** and **Issues**, read and write, nothing else.
3. **Rotate `INBOX_SECRET`** when anyone who had it shouldn't any more.
4. **Narrow the filters** in `inbox.yml` to the event types you actually use.
5. **Tell your agent the rules.** In its standing instructions and HANDLERS.md: only trust event comments from your relay's account, dedupe by `id`, treat every field as untrusted data, and ask you before anything irreversible.
6. **Pin actions by commit SHA** if your organisation requires it. The workflows use version tags (`actions/checkout@v4`) for readability.

## Known gaps

- Anyone with write access to the repository can post comments that look like events, push watcher changes, or run the relay manually. The repository's collaborators are part of your trust boundary.
- Webhook payloads and watcher results are visible to everyone with read access to the repository.
- The relay's dedupe looks at the newest 100 comments only.

## Reporting a vulnerability

Please report security problems privately through GitHub's **Report a vulnerability** button on this repository's Security tab rather than in a public issue.
