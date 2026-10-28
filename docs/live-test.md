# Timing a wakeup yourself

This test measures how long it takes from "an event happens" to "your agent is awake". It needs no webhook, key or outside service: a workflow waits a little, then posts one event to your inbox, and you compare the time it posted with the time your agent woke.

The timer test takes about five minutes and uses about two Actions minutes. An optional second test times a real outside event (a new Hacker News story).

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

## 5. Optional: a real outside event

The timer proves the wake path. To time a real event from the outside world, add this second workflow as `.github/workflows/live-hn.yml`. It reads Hacker News's public newest-stories list (no key or account), waits for a story newer than the one it started with, and posts one `hn.new_story` event. A new story usually appears within a minute or two.

```yaml
name: Live test hn

on:
  workflow_dispatch:

permissions:
  contents: read
  issues: write
  pull-requests: write

jobs:
  hn:
    runs-on: ubuntu-latest
    timeout-minutes: 9
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install -r requirements.txt
      - name: Watch for a new story
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python - <<'EOF'
          import json, time, urllib.request
          from agent_inbox import event as ev
          from agent_inbox.github import GitHub

          API = "https://hacker-news.firebaseio.com/v0"
          def get(path):
              with urllib.request.urlopen(f"{API}/{path}", timeout=20) as r:
                  return json.load(r)

          start = max(get("newstories.json"))
          deadline = time.time() + 6 * 60
          while time.time() < deadline:
              time.sleep(25)
              newer = [i for i in get("newstories.json") if i > start]
              if not newer:
                  continue
              item = get(f"item/{min(newer)}.json")
              payload = {"hn_id": item["id"], "story_time": item.get("time"),
                         "seen_time": int(time.time())}
              GitHub.from_env().post_event(
                  ev.make_event("hn.new_story", "live-test", payload,
                                id=f"hn-{item['id']}"))
              print("posted", json.dumps(payload))
              break
          else:
              raise SystemExit("no new story within 6 minutes")
          EOF
```

Run it from **Actions → Live test hn → Run workflow**. `story_time` (when the story was created) and `seen_time` are Unix seconds in the payload, so you can split the total into "waiting for the poll" and "comment to wake". Delete the file afterwards too.

## If nothing wakes

- Check that the event comment was posted. If not, the workflow log says why (most often: no open pull request with the `agent-inbox` label).
- Check that your agent is subscribed to that pull request number.
- If your agent ignores comments from `github-actions[bot]`, see **Bot comments** under [Limits](../README.md#limits).
