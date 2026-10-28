# Timing a wakeup yourself

This test measures how long it takes from "an event happens" to "your agent is awake". It needs no webhook, key or outside service: a workflow waits a little, then posts one event to your inbox, and you compare the time it posted with the time your agent woke.

It takes about five minutes and uses about two Actions minutes.

## 1. Set up the inbox

Follow steps 1 and 2 of the [README setup](../README.md#setup-about-ten-minutes), so you have a draft pull request labelled `agent-inbox`.

## 2. Point your agent at it

Start your agent watching the inbox pull request, with the prompt from [Connecting an agent](../README.md#connecting-an-agent). Add one line so the wake is recorded:

> When a `test.timer` event arrives, reply on the pull request with the time you woke up, in UTC.

## 3. Add the timer workflow

Save this as `.github/workflows/live-timer.yml` on your default branch (`main`). The **Run workflow** button only appears for files on the default branch.

```yaml
name: Live test timer

on:
  workflow_dispatch:

permissions:
  contents: read
  issues: write
  pull-requests: write

jobs:
  timer:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install -r requirements.txt
      - name: Wait
        run: sleep 75
      - name: Post the event
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
          python -m agent_inbox post --type test.timer --source live-test \
            --payload "{\"waited_seconds\": 75, \"posted_at\": \"$now\"}"
          echo "posted at $now"
```

The 75-second wait is there so the event lands while you are not touching anything, which is closer to a real event than pressing a button and watching.

## 4. Run it and read the times

Go to **Actions → Live test timer → Run workflow**. About a minute and a half later:

- The event comment appears on the inbox pull request. Its JSON holds `posted_at`, and GitHub shows the comment's own time when you hover over it.
- Your agent's reply shows when it woke.

The gap between the event comment and the agent's reply is your wake latency. Our own numbers are in the README under [Tested live](../README.md#tested-live).

Delete `live-timer.yml` when you're done, so nobody runs it by accident.

## If nothing wakes

- Check that the event comment was posted. If not, the workflow log says why (most often: no open pull request with the `agent-inbox` label).
- Check that your agent is subscribed to that pull request number.
- If your agent ignores comments from `github-actions[bot]`, see **Bot comments** under [Limits](../README.md#limits).
