Mission

Build Smart Table Analyzer exactly as defined in:

Architecture.md

Runtime_Environments_UI.md

These are the source of truth. Do not redesign the product unless a concrete implementation problem proves the architecture wrong.

Code measures. Storage remembers. Knowledge informs. The LLM investigates.

The only required user input is an Iceberg table name.

Pi Agent Routing

architect — terra

Primary orchestrator and integrator.

Owns:

task decomposition

architecture decisions

delegation

integration

ambiguous debugging

verification

final acceptance

Terra is not the default implementation agent.

For non-trivial implementation work, Terra must delegate through Pi's task/subagent tool instead of doing the feature work itself.

Terra may directly edit only:

tiny obvious fixes,

integration/glue after delegated work,

conflict resolution,

emergency unblock work that cannot reasonably be delegated.

Do not use Sol as orchestrator.

scout — ollama-cloud/deepseek-v4-flash

Use first for substantial investigation.

Owns:

repo exploration

tracing call paths

locating reusable code

dependency/API discovery

identifying affected files

Read-heavy only. Does not make architecture decisions.

worker — ollama-cloud/glm-5.3-flash

Default implementation agent.

Use for:

bounded feature work

tests

refactors

bug fixes

FastAPI/UI implementation

PyIceberg/DuckDB integration

documentation

hard-worker — ollama-cloud/kimi-k2.7-code

Use only when the normal worker is genuinely struggling with:

difficult cross-module implementation

complex debugging

non-trivial query/data logic

Do not use by default. Escalate to Kimi only when the GLM worker is genuinely struggling or the task is clearly complex.

reviewer — ollama-cloud/deepseek-v4-pro:cloud

Independent reviewer.

Use after meaningful changes.

Review for:

architecture violations

correctness

unnecessary complexity

regressions

missing tests

unsafe assumptions

Reviewer is read-only and must not implement the fix.

Delegation

AGENTS.md does not spawn agents automatically.

Terra must explicitly invoke Pi's task/subagent tool.

For every non-trivial implementation task, delegation is mandatory.

Default flow:

Terra
  ↓
scout          # when repo/code-path understanding is needed
  ↓
worker         # default code implementation
  ↓
hard-worker    # only if GLM worker struggles or task is clearly difficult
  ↓
reviewer       # meaningful code changes
  ↓
Terra integrates, verifies, accepts

Mandatory behavior

Before Terra implements non-trivial feature code:

Decompose the work into bounded tasks.

Invoke the appropriate Pi subagent through the task tool.

Give the agent:

exact objective,

owned files/module,

relevant architecture invariants,

expected output,

tests/verification required.

Wait for the delegated result.

Inspect the diff/output.

Integrate or request a correction.

Run the reviewer after meaningful changes.

Terra performs final end-to-end verification.

Who writes code

scout       → read-heavy exploration; no feature implementation
worker      → default implementation
hard-worker → difficult implementation/escalation
reviewer    → read-only review
Terra       → orchestration, integration, verification

Terra must not silently take over a worker task because it looks faster to implement directly.

If a delegated worker fails:

retry with a clearer bounded task, or

escalate to hard-worker.

Only then may Terra make the minimum integration/unblocking correction itself.

Direct-work exception

Terra may work directly only when the task is genuinely trivial, such as:

single obvious config correction
small documentation edit
tiny integration fix
merge/conflict cleanup

Building a feature, backend module, query tool, UI flow, test suite, refactor, or multi-file change is not trivial and must be delegated.

Concurrency

Ollama Pro allows at most 3 concurrent Ollama model sessions.

Never exceed 3 active Ollama subagents.

Concurrency is a limit, not a target.

Prefer sequential delegation when tasks depend on each other.

Parallelize only genuinely independent work.

No nested delegation.

No swarms.

One agent owns writes to a module/subtree at a time.

Delegation failure

If Pi's task/subagent tool is unavailable or delegation cannot be started:

do not quietly continue by having Terra implement the whole task.

Report the delegation problem clearly, then either fix the Pi delegation setup or ask for direction.

Before Coding

Read the relevant parts of:

Architecture.md
Runtime_Environments_UI.md

Then inspect:

current implementation
tests
call sites
existing reusable code

Prefer adapting existing code over creating parallel systems.

STA Hard Invariants

Never violate these:

User supplies only catalog.schema.table.

LLM never writes SQL.

Query tools execute reviewed predefined queries only.

Tools measure only; never diagnose, summarize, score, or recommend.

Every query result is persisted as Rxxx.

Stored query results are the evidence.

Investigator performs all interpretation and recommendations.

Knowledge = Iceberg + IOMETE + team runbooks.

Raw/large DDL is not dumped into model context.

Startup TableContext is compact and metadata-derived.

Use Iceberg metadata before table scans.

Never broadly profile all columns.

Identifier-like columns are excluded from expensive partition-candidate analysis.

Workload analysis is disabled for MVP.

NO CHANGE and INCONCLUSIVE are valid outcomes.

Immediate remediation and future table design are separate.

Production = IOMETE/Spark/Iceberg.

Local = Docker Iceberg + PyIceberg + DuckDB.

Local/production parity is enforced at tool-result contracts, not SQL syntax.

UI = plain HTML/CSS/JS.

Live progress = SSE.

Expose observable actions only, never hidden chain-of-thought.

Engineering Style

Prefer the smallest correct solution.

Avoid:

speculative abstractions

duplicate layers

unrelated refactors

feature creep

temporary patches hiding root causes

framework-heavy designs

new infrastructure without a demonstrated need

Do not introduce:

React/Vue/Angular
Redis/Celery/Kafka
microservices
arbitrary SQL tools
recommendation engines
critic agents
planner/hypothesis frameworks
broad column profilers

unless the architecture explicitly changes.

Codebase Structure & Hygiene

Keep the repository modular, predictable, and easy to navigate.

Folder ownership

Use clear domain boundaries:

src/sta/
├── app/            # FastAPI entrypoints, run lifecycle, SSE
├── investigator/   # Pydantic AI agent, prompt, report schema
├── context/        # TableContext and schema compression
├── tools/          # model-facing deterministic query tools
├── execution/      # QueryRunner, backends, query templates
├── results/        # Rxxx models, persistence, retrieval
├── knowledge/      # search/read implementation
├── iceberg/        # Iceberg metadata/catalog helpers
├── ui/             # HTML/CSS/JS only
└── observability/  # logging/tracing

knowledge/
├── iceberg/
├── iomete/
└── runbooks/

tests/
├── unit/
├── integration/
├── contract/
└── e2e/

Tests should roughly mirror the source area they validate.

Modularity rules

One module should have one clear responsibility.

Prefer small cohesive modules over large catch-all files.

Split a file when it starts owning multiple independent concerns.

Do not split trivial logic into unnecessary one-class files.

Keep public interfaces small and explicit.

Avoid circular imports.

Domain code must not depend on UI code.

Investigator code must not contain backend-specific SQL.

Query tools must not contain storage or recommendation logic.

Backend-specific behavior belongs under execution/backends/.

Backend-specific SQL belongs under:

execution/queries/iomete/

execution/queries/local/

Shared Pydantic result contracts must remain backend-independent.

Keep knowledge documents outside Python packages.

File naming

Use descriptive snake_case names.

Good:

table_context.py
file_layout.py
partition_analysis.py
result_store.py
iomete.py
local.py

Avoid:

utils.py
helpers.py
common.py
misc.py
manager.py
service2.py
new_code.py

If shared helpers are genuinely needed, name them after the domain they serve.

File size / complexity

Do not enforce arbitrary line limits, but treat a file as suspicious when:

unrelated responsibilities accumulate,

navigation becomes difficult,

tests require many unrelated fixtures,

changes repeatedly touch distant logic in the same file.

Refactor by responsibility, not by line count.

Imports and formatting

Use project-standard formatter/linter consistently.

Keep imports ordered and remove unused imports.

No wildcard imports.

Prefer explicit types at module boundaries.

Use Pydantic models for external/tool/result contracts.

Keep functions focused and names descriptive.

Comments explain why, not obvious syntax.

Remove dead code instead of commenting it out.

Repository cleanliness

Do not leave:

temporary scripts
debug dumps
duplicate implementations
unused feature flags
old architecture files referenced by active code
generated test artifacts
*.bak / *_old / *_new copies

Put one-off development scripts under scripts/ and keep only scripts that remain useful.

Before finishing structural work:

check git diff
check git status
remove dead/duplicate files
verify imports
verify tests still match package structure

Creating new folders

Create a new folder only when it represents a real architectural boundary or contains multiple related modules.

Do not create deep nesting such as:

src/sta/core/services/managers/handlers/implementations/

Prefer shallow, obvious paths.

A developer should be able to locate a feature from its domain name without searching the whole repository.

Validation

For every meaningful change:

Run the narrowest relevant tests.

Run broader affected tests.

Exercise the real call path when practical.

Inspect outputs/events/results, not only exit codes.

Verify local/production tool contracts when touching query tools.

Run reviewer.

Architect inspects the final diff and reviewer findings.

Do not claim completion while known failures remain.

For runtime/UI work verify:

table input
→ run created
→ progress streams
→ tool executes
→ Rxxx stored
→ result view works
→ final report renders

Progress

Maintain:

docs/implementation-progress.md

Keep it short:

Completed
In progress
Blocked
Next

Update only after meaningful milestones.

Completion Standard

A task is complete only when the original user goal works end-to-end.

Do not stop at:

implementation added
worker finished
tests mostly pass
backend works but UI does not

Fix root causes, verify the real workflow, review the diff, then mark complete.