# Limits: where a listener may be refused

agent-inbox makes it easy to build listeners. Agent platforms, GitHub and the sites you watch set rules about which listeners are fine. This page gives rough bounds, not legal advice. Each item is marked:

- **Documented**: the linked page says so (read October 2026; pages change, so check them).
- **Inferred**: our reading, not something a source states.
- **Observed**: something that happened to us, with the cause unknown.

## The short version

| Listener | Likely outcome |
|---|---|
| Timers, public feeds, news lists, weather, prices, showtimes, your own services' webhooks | Fine, within the site's terms and GitHub's limits |
| One specific person's account or activity (posts, likes, follows, location), famous or not | Expect refusals from agent platforms and from the site |
| Pages behind a login | Not supported, and often against the site's terms |
| Buying or reserving automatically when an event fires | Against many sites' terms; agents should tell you and let you buy |
| Polling every few seconds | Not possible on GitHub's schedule, and bad manners towards the site |

## Watching specific people

- **Documented.** Anthropic's Usage Policy bars using its products to track or monitor a person without their consent, including their online activity and communications. The exceptions cover consented tracking, aggregated or anonymised analysis, and some security, legal and journalism work. Personal alerts about a public figure are not among them. [Usage Policy](https://www.anthropic.com/legal/aup) (the page showed an effective date of November 12, 2026 when we read it).
- **Documented.** Anthropic's guidance for agents says they must not monitor people's online activity, behaviour or movements without notice or consent. [Using agents according to our Usage Policy](https://support.claude.com/en/articles/12005017-using-agents-according-to-our-usage-policy)
- **Documented.** X's developer agreement bars using X content to track users or their content without X's written approval. [X Developer Agreement](https://docs.x.com/developer-terms/agreement)
- **Observed.** In our own testing, a Claude Code cloud session's safety check refused one brief. That brief combined pushing to the user's own private repository with watching a very famous person's social media posts and likes. No reason was given. A brief with a timed event and a Hacker News new-story watcher was allowed, and both listeners worked. **The cause of the refusal is unconfirmed.** It may have been the person-watching part, the combination, or something else.
- **Documented.** Claude Code describes a separate safety filter that can refuse an action because of earlier content in the conversation, without naming a reason. Retrying the same thing won't help. [Claude Code errors](https://code.claude.com/docs/en/errors)
- **Inferred.** Point watchers at topics, products, feeds and events, not at a person. "New posts on this project's blog" is a feed. "Everything this person likes" is monitoring a person.

## Sites that forbid scraping

- **Documented (press report).** Since September 2023, X's terms ban crawling and scraping without prior written consent. We couldn't load X's own terms page, so this rests on [TechCrunch's report](https://techcrunch.com/2023/09/08/x-updates-its-terms-to-ban-crawling-and-scraping).
- **Documented.** An earlier version of Ticketmaster's terms bans bots and scrapers, and bans bots that search for, reserve or buy tickets. It sets request caps and charges per request over them. [Ticketmaster terms, prior version](https://legal.ticketmaster.com/terms-of-use/prior-version-terms-of-use/)
- **Inferred.** Each site's own terms and robots.txt decide whether a watcher is welcome. Prefer an official API or feed. Big sites often block requests from cloud IP ranges, including GitHub's runners, even when their terms would allow the request. A watcher that suddenly matches nothing counts as an error, so you'll hear about it.

## Data behind a login

- **Documented.** The Usage Policy bars accessing private data unlawfully. Anthropic's agent guidance bars using stored credentials to get into someone else's account. [Usage Policy](https://www.anthropic.com/legal/aup), [agent guidance](https://support.claude.com/en/articles/12005017-using-agents-according-to-our-usage-policy)
- **Documented.** Claude Code's auto mode blocks writing live credentials into files or the transcript, and blocks copying credential stores into commits. [Permission modes](https://code.claude.com/docs/en/permission-modes)
- **Inferred.** Watchers deliberately don't support logins. Putting a session cookie in a repository secret would break most sites' terms, and an agent will likely refuse to set it up for you.

## Acting on events (buying, booking)

- **Documented.** Claude Code's auto mode blocks irreversible actions, and actions aimed outside your environment, unless your message names that exact action. [Auto mode](https://code.claude.com/docs/en/auto-mode-config)
- **Documented.** Anthropic's agent guidance bars unauthorised financial transactions. [Agent guidance](https://support.claude.com/en/articles/12005017-using-agents-according-to-our-usage-policy)
- **Inferred.** Write handlers that tell you and let you decide. HANDLERS.md already says "Don't buy anything".

## GitHub's limits

- **Documented.** The shortest schedule is every 5 minutes. Scheduled runs can start late under load, and some can be dropped. Schedules in public repositories turn off after 60 days with no activity. Manual runs and `repository_dispatch` only work for workflow files on the default branch. [Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- **Documented.** `GITHUB_TOKEN` gets 1,000 API requests an hour per repository. [Actions limits](https://docs.github.com/en/actions/reference/actions-limits)
- **Documented.** Content creation, such as posting comments, is capped at 80 requests a minute and 500 an hour. When GitHub sends a `retry-after` header, wait that many seconds. [REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api). agent-inbox posts at most 20 events per watcher per run and honours `retry-after`.
- **Documented.** GitHub's Acceptable Use Policies bar excessive automated bulk activity. [GitHub AUP](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)
- **Documented.** Private repositories get a monthly quota of Actions minutes (2,000 on Free), and each job is rounded up to a whole minute. With no payment method on file, runs stop once the quota is used up. [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)

## Agent platform limits (Claude Code)

- **Documented.** In auto mode, the classifier trusts only the working repository and its remotes. A repository being private doesn't make it fine to store personal data there. [Auto mode](https://code.claude.com/docs/en/auto-mode-config)
- **Documented.** Cloud sessions reach only allowlisted domains by default, and their GitHub access is limited to repositories attached to the session. [Cloud environments](https://code.claude.com/docs/en/cloud-environments.md)
- **Inferred.** Watchers run in GitHub Actions, not inside the agent, so the agent's network allowlist doesn't stop a watcher. It only stops the agent from trying the watched site itself.
- **Observed.** A cloud session subscribed to the inbox pull request was woken by comments from `github-actions[bot]`, 2 to 3 seconds after each comment, in a private repository and again in this public one ([pull request #1](https://github.com/vexxxing/agent-inbox/pull/1)).

## Seen in the wild

We'd rather collect real cases than guess. If a listener is refused in normal use, open an issue or pull request adding one row below. Please don't go looking for refusals by trying variations: that's working around a safety check, not mapping it.

| Date | Platform or site | What the listener watched | What was refused | Message shown |
|---|---|---|---|---|
| 2026-10-27 | Claude Code (cloud) | A very famous person's social media posts and likes, with the agent pushing setup to the user's private repo | The whole brief, before any work started | None |
| 2026-10-27 | Claude Code (cloud) | A timer, then Hacker News's newest stories | Nothing: both ran and woke the agent | |
| 2026-10-28 | Claude Code (cloud) | The same two tests in this public repository | Nothing: both ran and woke the agent | |
