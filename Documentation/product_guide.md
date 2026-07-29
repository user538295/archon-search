# archon-search — A Beginner's Guide

This guide explains what archon-search is, what it's for, and how it works, in plain English. It assumes no background in search engines, databases, or AI. Every technical term is explained the first time it appears.

---

## What is archon-search?

archon-search is a small program you run on your own computer that turns a folder of your files into something you can ask questions of.

Imagine you have hundreds of notes, PDFs, manuals, and saved articles scattered around. With archon-search you can ask, in plain English:

> "How do I cancel my subscription?"
> "What did we decide about the pricing change in March?"
> "Where in this codebase do we handle retries?"

…and get back the most relevant paragraphs from your own files, with the file names so you can click through to read more.

Two things make it different from regular search:

1. **It understands meaning, not just words.** A search for *"cancel my plan"* will find a paragraph titled *"ending your subscription"*, even though none of the words match.
2. **It runs on your computer.** Your documents never leave your machine. There is no cloud account and no upload.

You can use it yourself, or let an AI assistant (like Claude) use it on your behalf — so the assistant can answer questions based on *your* notes instead of guessing.

---

## What problem does it solve?

You probably have a lot of text saved somewhere: notes, articles, emails, PDFs, code. Finding the right bit months later is hard.

- **Spotlight or Windows Search** only matches the exact words you typed. If you don't remember the words, you don't find the file.
- **AI chatbots** are great at understanding what you mean, but they don't know your private notes — and if you upload them, your data leaves your machine.
- **Building your own AI search** is technically possible but requires assembling many tools yourself.

archon-search sits in the middle: smart search like a chatbot, working on your own files, with nothing uploaded anywhere.

---

## What can you do with it?

- **Ask questions in plain English** and get the most relevant passages back, with source files.
- **Group your files into "collections"** — for example: `work-notes`, `recipes`, `research`. Each is searched independently, or all together.
- **Let it watch your folders.** When you add, edit, or delete a file, archon-search updates itself automatically.
- **Connect AI assistants to it.** Tools like Claude Code can use archon-search to look things up in your material before answering. This uses a standard called **MCP** (Model Context Protocol), which is just a shared way for AI tools to talk to other tools.
- **Use it from scripts** through a normal web-style interface (a REST API), so other programs can search too.
- **Keep multiple users separated** with namespaces and per-file access labels, if you share an instance.

---

## How does it work? (a simple picture)

When you first point archon-search at a folder, it does four things to each file:

1. **Read** the file (Markdown, PDF, plain text, code, etc.) and pull out the words.
2. **Chop** long documents into smaller, paragraph-sized pieces. Searching whole 50-page PDFs doesn't work; searching paragraphs does.
3. **Make a "meaning fingerprint" for each piece.** A small AI model turns each paragraph into a list of numbers that captures *what the paragraph is about*. Two paragraphs about the same topic get similar fingerprints, even if they use different words.
4. **Store** the paragraph, its fingerprint, and a normal word-index in a small database on your disk.

When you ask a question:

1. The same AI model turns your **question** into a fingerprint.
2. archon-search looks for paragraphs whose fingerprints are closest to the question's (the "meaning" search) **and** paragraphs that contain your actual words (the "keyword" search). Doing both at once is called **hybrid search** and it works much better than either one alone.
3. A second, more careful model looks at the top candidates and the question side-by-side and re-orders them so the best match ends up on top.
4. You get back the best paragraphs, each with the file it came from.

That's the whole idea: **read → chop → fingerprint → store → search → re-rank**.

If you have many collections, an extra step called the **router** decides which collections are likely to contain the answer, so it doesn't waste time searching unrelated ones.

---

## What technologies does it use, and why?

You don't need to know any of this to use archon-search, but if you're curious, here's what's under the hood and why each piece was chosen.

### Python
The language the whole thing is written in. Python has the largest collection of ready-made tools for working with text and AI models, so it lets archon-search reuse a lot rather than build from scratch.

### FastAPI (the "web" part)
The piece that listens for requests over HTTP. Chosen because it produces a self-documenting interface — when you change the code, the documentation updates automatically.

### LanceDB (the database)
Where your paragraphs, their fingerprints, and metadata are saved. Most "AI search" databases require running a separate server; LanceDB is just a folder of files on your disk. Easier to install, easier to back up (you can literally copy the folder), and it does both meaning-search and keyword-search in one place.

### fastembed (the "meaning fingerprint" model)
The small AI model that turns text into number-fingerprints. The default model is about 33 MB and runs locally on your CPU — no internet needed, no API key, no cost per query. Picking this over a cloud model is what makes archon-search private and free to run.

### Cross-encoder reranker (the "double-check" model)
A second, more careful model that takes the top results from the first search and re-ranks them by reading the question and each result together. Slower than the first model, but more accurate. Running it only on the shortlist gives the best of both worlds: speed *and* precision.

### ONNX Runtime (the model runner)
A small piece of software that actually runs both AI models above. Chosen because it works on any CPU, and can optionally use a GPU if you have one, without changing anything else.

### Watchdog (the file watcher)
Watches your folders for changes. When you save a file, it tells archon-search to reindex it. Without this you would have to manually re-import after every change.

### MCP / FastMCP (the AI-assistant bridge)
The standard interface that lets AI assistants like Claude talk to archon-search. This is what turns archon-search from "a search server I can call from a script" into "long-term memory for my AI assistant".

### Bearer-token authentication
A simple security model: every request must include a long random "API key" that proves you're allowed to use the server. On first run, archon-search generates a strong key for you and stores it in a file only your user account can read. So even though it runs on your machine, no random program or browser tab on your computer can read your documents through it.

### `uv` and `hatch-vcs` (developer tooling)
Used to manage dependencies and version numbers automatically. You don't see these unless you're working on archon-search's code itself.

---

## Why this combination is appropriate

Each piece reinforces the others:

- **Local-first.** LanceDB stores everything on your disk; fastembed runs the AI on your CPU. Together they mean archon-search works without internet and without sending anything anywhere.
- **Hybrid search in one place.** Because LanceDB does both meaning-search and keyword-search natively, archon-search doesn't have to stitch two databases together.
- **Two-stage search.** The cheap first stage narrows millions of paragraphs to dozens; the careful second stage picks the winners. Fast *and* accurate.
- **One server, two audiences.** The same content is reachable by humans (through the API and CLI) and AI assistants (through MCP), with the same security rules.
- **Safe defaults.** Authentication is on by default. Telemetry (usage logging) is off by default. When you turn it on, it stays on your computer.

The result is a system where each part is a sensible everyday choice, but together they cover a lot of ground.

---

## Who is it for?

- **People with lots of notes** (researchers, writers, students) who want to ask their own library questions by meaning, not just keyword.
- **Developers** who want their AI coding assistant to actually understand their own codebase.
- **Small teams** who want a private knowledge base without sending their docs to a cloud service.
- **Anyone** whose work is sensitive enough that uploading documents isn't an option — medical, legal, journalistic, or just personal.

It is **not** meant for searching the public internet, running across a cluster of machines, or replacing a full enterprise search system.

---

## What it does *not* do

- It does **not generate written answers** for you. It returns relevant passages; an AI assistant on top can compose them into prose.
- It does **not transcribe audio or video.** Convert to text first.
- It does **not encrypt your files on disk.** Normal file-system permissions are the boundary.
- It does **not crawl the web.** Save the page locally, then ingest.

---

## Where to go next

If you'd like to try it:

- **Five-minute setup:** `Documentation/quick_start.md`
- **Install and configure:** `Documentation/UserManual/10_installation.md` → `02_configuration.md`
- **Add documents:** `Documentation/UserManual/50_ingestion_and_collections.md`
- **Search your documents:** `Documentation/UserManual/60_searching.md`
- **Connect an AI assistant:** `Documentation/DeveloperGuide/05_mcp_integration.md`

If you'd like to understand the inside in more depth:

- **Architecture overview:** `Documentation/Architecture/100_system_architecture_overview.md`
- **Design decisions:** `Documentation/ADRs/`
