# Security

Ucho runs locally and handles recordings of private conversations, so security reports are welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead: the *Security* tab of this repository → *Report a vulnerability*.

You can expect a first answer within 7 days. Ucho is maintained by one person, so fixes may take longer; you will be kept informed.

## In scope

- Anything that makes Ucho send audio, transcripts, settings or logs over the network.
- The MCP server reading files outside the recordings folder, or being reachable by anything other than the process that started it.
- Model or package download that could be redirected to unpinned or tampered files.
- Code execution through crafted recordings, `meta.json`, settings files or transcripts.

## Out of scope

- What an AI agent connected through MCP does with transcript text it has read (that is governed by the agent and its provider).
- Physical access to an unlocked computer.
