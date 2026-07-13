# Durable campaign persistence v1

`CampaignPersistence` stores the W-0051 bounded-campaign contracts in the same SQLite
database family as Factory and Muster. Its `factory_campaign_events` stream is the
canonical source: events are ordered per campaign, bind an idempotent operation ID, and
form a SHA-256 chain over canonical payloads. Campaign, epoch, lease, checkpoint, and
operator-decision tables are transactional projections rather than a second history.

Every public mutation is one SQLite transaction. Deterministic failure callbacks cover
the event and projection write boundaries; an interruption exposes either the entire
operation or none of it. Reusing an operation ID with exact content returns the committed
result, while different content fails closed. Checkpoint commits atomically publish the
checkpoint, release its lease, update the epoch, and advance cumulative usage. Usage must
be monotonic, its delta must fit the epoch budget, and its total must fit the campaign
ceiling. Completed/exhausted outcomes must reference a checkpoint owned by that campaign;
pre-epoch stop/failure outcomes may explicitly carry none and never invent one.

Epochs may only be published and leased while a campaign is running. After restart,
`recover()` changes any surviving active lease to `recovery-required` and moves the
campaign to `waiting`; it never infers completion from a missing controller or worker.
Operator intervention or later controller policy must explicitly resolve that state.

`rebuild_projection()` replays canonical history without modifying it, verifies event
sequence/hash continuity and contract identities/transitions, checks epoch/checkpoint
references, and compares replay with the live projection. Gaps, duplicate operations,
tampering, dangling references, impossible transitions, and stale projections are returned
as a degraded rebuild report. All reads and writes are bound by `campaign_id`, preventing
one concurrent campaign from leasing, checkpointing, spending, approving, or terminating
another campaign's authority.
