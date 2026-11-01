# Quickstart

Ten minutes from nothing to an agent that reads your documents, asks before it writes, and remembers. No API key or model download needed: a fresh workspace has a built-in **demo model**. It is deterministic rules, not a language model, and every answer it gives starts with `[demo]`. It exists so you can see tools, approvals, memory and knowledge work before you connect a real model.

## 1. Create a workspace

```bash
mkdir acme && cd acme
hollis init
```

This creates `hollis.toml` (configuration, safe to commit), `.hollis/` (databases, ignored by git), a starter agent called `assistant`, and a knowledge collection called `docs`.

## 2. Run the agent

```bash
hollis run "list the files here"
```

You'll see the run's events as they happen: the model call, the `fs_list_dir` tool call (allowed by the default policy, because it is a trusted read-only tool), and the answer.

## 3. Give it documents

```bash
printf '# Release process\n\nReleases ship on Tuesdays. Roll back with make deploy TAG=<previous>.\n' > release.md
hollis knowledge add docs release.md
hollis run "How do we roll back a release?"
```

The answer cites the passage it used (`[C-…]`), and the run records a grounding report: whether each claim's citation points at text that supports it.

## 4. See an approval

```bash
hollis run "write notes.txt: remember to tag the release"
```

Writing a file is a mutating action, so the default policy holds it for a person. At a terminal, Hollis shows the exact call (`path`, `content`) and asks. Answer `y`, and the run continues; answer `n`, and the agent is told it was denied. In a script, use `--approve stop` to leave the run waiting and decide later:

```bash
hollis run --approve stop "write todo.txt: ship it"
hollis approvals list
hollis approvals approve <run-id>
```

## 5. Open the web app

```bash
hollis serve
```

The server prints a sign-in link that works once, for ten minutes. Open it to see the overview, follow runs live, decide approvals, and manage agents, models, knowledge and memory. Leave the server running: in another terminal, `hollis` commands in this workspace now go through it, and runs started from the browser keep going when you close the tab.

## 6. Connect a real model

For example, a local Ollama:

```bash
hollis models add local-qwen --protocol openai-chat-completions --model qwen3:8b \
  --base-url http://127.0.0.1:11434/v1 --tools --context-window 32768
hollis agents update assistant --model local-qwen
hollis models probe local-qwen      # one tiny request to check it answers
```

For a hosted API, name the environment variable that holds the key (`--auth-env OPENAI_API_KEY`); Hollis stores the name, never the value. Hollis has not been verified live against any server yet; the [compatibility matrix](../architecture/model-compatibility.md) says what is documented and what is verified.

## Next

- [CLI reference](../reference/cli.md) · [SDK guide](sdk.md) · [Web app](web.md)
- [Configuration reference](../reference/configuration.md) · [REST API](../reference/rest-api.md)
- [Running a shared server](deployment.md)
