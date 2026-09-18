See [`CLAUDE.md`](CLAUDE.md) for the complete agent instructions, repository map, evidence
precedence, gap-closing workflow, and validation commands. Its instructions are mandatory.

Before changing the impersonator, read these in order:

1. [`CLAUDE.md`](CLAUDE.md)
2. [`docs/firmware-implementation-gap-tracker.md`](docs/firmware-implementation-gap-tracker.md)
3. The relevant sections of [`docs/protocol.md`](docs/protocol.md) and
   [`docs/dead-ends.md`](docs/dead-ends.md)

Work against a named `GAP-*` item. Direct 3.1.6 decompiler evidence cited by the tracker takes
precedence over conflicting older prose. Do not guess fields marked `descriptor required`, and do
not close a gap without its tests and evidence record.

Local source edits and tests do not authorize deployment, live backend mutation, token rotation,
reboots, or firmware flashing. Perform those only when the user explicitly requests them. Live
Pi/camera access is in the git-ignored `.agent/pi-ops.md` (template:
`.agent/pi-ops.example.md`).
