# Entry & Import (UI milestone 2) — design

**Status:** proposed, 2026-08-24. Supersedes nothing; extends
`2026-08-19-read-only-api-design.md`, which named this milestone out of scope.

The first writes in the UI. Milestone 1 made the API read-only *at the Postgres
level* and called that "a guarantee, not a review convention" (D3). This
milestone has to add writes without spending that guarantee.

---

## 1. Decisions

| # | Decision | Why |
|---|---|---|
| E1 | A **second, write-enabled pool** in the same app. Read handlers keep the read-only pool. | Reads keep the Postgres-level guarantee where it earns its keep — a bug in a read handler still cannot write. One process, one deploy. |
| E2 | Manual entry creates **fills**, never trades. Grouping derives trades as it does for imports. | §5 makes fills ground truth. One derivation path means a hand-entered trade and an imported one are indistinguishable downstream, which is what makes P&L trustworthy. Manual *grouping* stays a later increment. |
| E3 | Scope is **manual entry, multi-leg builder, CSV wizard, and marks/snapshot forms**. | The first three are §8 screen 5. Marks and snapshots are small (the CLI backend exists) and are the reason the Dashboard shows dashes for equity, unrealized P&L and drift. |
| E4 | The wizard supports **both Fidelity dialects, with detection**. | Both already parse, and `content_hash` dedupe makes a repeat import idempotent. The real difference is account routing, which is one branch in the flow, not two designs. Settles gap R5. |
| E5 | A **manual** fill can be deleted, then the account regroups. Imported fills are immutable. | A hand-typed fill needs an undo or a typo is permanent. Imported fills are reproducible from the export, so deleting them invites divergence from the source of truth. |
| E6 | **CLI first.** Write logic lives in `db/`; CLI commands wrap it; API endpoints call the same functions. | Every write in this repo is a CLI command and `tests/db/test_cli.py` is where write coverage lives. Keeps one code path and one test surface. |
| E7 | **Write routes are not registered on the tailnet-served instance.** | See §6. This is the only enforcement that survives `tailscale serve`. |

---

## 2. Write plumbing

`api/deps.py` gains `get_write_conn`, drawing from a lazily-created
`app.state.write_pool` built **without** `default_transaction_read_only`.
`app.state.pool` is untouched.

Every write endpoint wraps `write + regroup_account` in a single
`async with conn.transaction():`, matching the import path — `db/importing.py`'s
module docstring already documents that contract, and `cli.py:1101-1108` is the
worked example. Consequences, both wanted:

- A fill can never land ungrouped.
- A failing regroup rolls the fill back rather than leaving the ledger in a
  state where the fill exists but no trade reflects it.

**Two structural guards, not conventions:**

1. `tests/api/test_readonly.py` already pins that the read pool refuses writes.
   It stays, unchanged.
2. A new test walks `app.routes` and asserts the pool dependency matches the
   HTTP method: a `POST`/`DELETE` that forgets `get_write_conn` fails CI, and a
   `GET` that reaches for the write pool fails too. The separation is checked by
   a machine, not by a reviewer remembering.

---

## 3. The write surface

### `db/`

- `add_manual_fills(conn, account_id, fills)` — inserts N fills in one call.
- `delete_manual_fill(conn, fill_id)` — refuses a non-manual fill **in SQL**
  (`DELETE FROM fill WHERE id = $1 AND source = 'manual'`), so a caller that
  forgets to check cannot bypass it. Returns whether a row went.

### CLI

`fills add` and `fills rm`. `marks set` and `snapshot add` already exist and are
reused untouched.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/fills` | Takes a **list**. Multi-leg is N fills in one transaction. |
| `DELETE` | `/api/fills/{id}` | Manual-source only; `409` on an imported fill. |
| `POST` | `/api/marks` | Wraps `marks set`. |
| `POST` | `/api/snapshots` | Wraps `snapshot add`. |
| `POST` | `/api/imports/preview` | Parses an upload, returns the batch summary and the duplicate report. Read-only; uses the read pool. |
| `POST` | `/api/imports/commit` | Re-parses and commits. |

**Commit re-parses the uploaded file rather than trusting a batch handed back by
the client.** No server-side session state to expire or leak, and `content_hash`
makes it idempotent — committing twice is a no-op, not a double import. The cost
is parsing twice, which is milliseconds.

---

## 4. Screens

One route, `/entry`, with a fixed segmented control: **Fill · Multi-leg ·
Import · Marks**. No rearrangeable panes (D11).

### Fill

Keyboard-first is a concrete claim, not an adjective: one row of fields in fill
order — account, symbol, side, quantity, price, fee, executed at. Tab advances,
Enter submits and returns focus to the first field **retaining account and
date**, so eight fills are eight passes with no mouse. Below, the fills added
this session, newest first, each with a delete. Imported fills never show one.

An unknown symbol mints an instrument. That path **validates a non-empty,
trimmed symbol** and shows an explicit "this will create a new instrument"
confirmation. Issue #27 is exactly an instrument minted with `symbol = ''` that
nobody noticed; manual entry must not be a second way to do that.

### Multi-leg

The same row repeated, with account and executed-at hoisted out as shared header
fields, plus expiry / strike / right per leg. Submits every leg as one
`POST /api/fills` in a single transaction, so a four-leg position is atomic —
never two legs in and two rejected.

This creates **fills**, and the grouper decides the rest. It never produces
`Direction.SPREAD`: `ledger/grouping.py:187` only ever assigns LONG or SHORT, and
`tests/test_grouping_properties.py:197,223` assert that. So the builder is a
typing convenience, and the `NotImplementedError` at `ledger/pnl.py:66,239`
stays dormant rather than becoming reachable.

### Import

Three steps.

1. **Pick** file and venue.
2. **Preview**, rendered straight from `ImportBatch`: counts for
   fills/cash/transfers, `warnings`, `unmapped_rows`, the duplicate report, and
   `blocking` **grouped by account ref** — `blocking` is `(account_ref, message)`
   pairs, so the screen can say *this* account's rows block while *that*
   account's are fine. Account routing lives here too: `refs_seen` lists every
   ref in the raw file (including accounts whose rows are entirely unmapped),
   and any ref without a matching account gets a selector. The History dialect,
   having no account column, presents one selector for the whole file.
3. **Commit**, disabled while any ref being imported still has a blocking
   reason. Afterwards, a summary of rows written and trades regrouped.

### Marks

Two small forms over `marks set` and `snapshot add`. The smallest part of the
milestone and the one that lights up equity, unrealized P&L, drift, and the
Accounts screen's rules panel.

---

## 5. Validation and errors

**Money and quantities cross the wire as strings in both directions.** The read
path already renders Decimals as fixed-point strings app-wide; the write path
accepts strings and must never let JavaScript parse them into a `Number`. A
float round-trip silently destroys a satoshi-scale value — the exact failure
`web/src/format.ts` exists to avoid on the display side. Inputs are
`type="text"` with an `inputmode`, never `type="number"`.

Server-side validation is authoritative; the client only avoids obvious round
trips. `422` for validation failures, `409` for deleting an imported fill, both
rendered inline against the offending field rather than as a banner.

---

## 6. Exposure — why writes are not registered on the served instance

The app "binds 127.0.0.1 only and contains no auth code" (D1). That was a sound
trade for a read-only service. It is not sound for writes, because in practice
the app is **not** only on localhost.

The deployment (details in `docs/ops/`, which is gitignored — this repo is
public and carries no infra topology) publishes it through a Tailscale-proxied
HTTPS endpoint. Two properties matter here, and both were verified against the
live deployment on 2026-08-24 rather than assumed:

- **The proxy forwards `/`** — every path, with no path-scoped exclusion. Writes
  would be exposed the moment they exist.
- **The tailnet is shared with other accounts.** It is not a private LAN, and
  the repo's operating notes already say to treat it as semi-trusted.

**A `request.client.host == "127.0.0.1"` check does not work here, and must not
be used.** The proxy is the client, so a proxied request already presents as
`127.0.0.1`. Such a check would pass for exactly the requests it was written to
stop — a guarantee that is not one.

**The enforcement instead:** `create_app()` registers the write routers only
when `DEADBAND_ENABLE_WRITES` is set. The published unit does not set it, so the
write endpoints **do not exist** there and return `404` to every proxied
request, regardless of who asks. Nothing is trusted — not a header, not a source
address, not a hostname.

Writes are served by a second unit on a **different local port that the proxy
does not publish**, reached over an SSH tunnel. The claim is verifiable in one
command by anyone at any time: a `POST` to a write path on the published
endpoint returns `404`.

**Ops consequence, called out because it is not free:** this adds a second
systemd unit and a tunnel step to the deployment kit under `docs/ops/`.

**Separately, and not solved by this design:** the runbook requires confirming
in the Tailscale admin console that the ACL restricts this node's web serve to
Michael's own devices, and calls that step "not optional". It is manual, its
state cannot be read from the build host, and it governs the **read** exposure
that is already live. Worth confirming independently of this milestone.

---

## 7. Testing

Coverage lands where this repo already puts it — `db/` and the CLI — with the
API layer kept thin.

- **`db/`**: `add_manual_fills`; `delete_manual_fill` refusing a `csv`-sourced
  fill; and a rollback test proving a failing `regroup_account` takes the fill
  with it rather than leaving it orphaned.
- **CLI**: `fills add` / `fills rm` in `tests/db/test_cli.py`.
- **API**: contract tests per endpoint, plus the route/pool dependency guard from
  §2, plus a test that write routes are **absent** when `DEADBAND_ENABLE_WRITES`
  is unset — the §6 control, pinned.
- **Web**: `tsc -b && vite build` via the existing CI lane.

---

## 8. Out of scope, named

- **Manual grouping** (`grouping_mode='manual'` with real allocations). The
  `group_fills` gate that blocked it is closed (`cefd27d`), so it is now safe —
  but it is the riskiest thing in §8 and does not belong in the same milestone
  as the write plumbing itself.
- Editing a fill in place. Delete and re-enter; an edit is a delete-and-insert
  underneath (`content_hash` changes) and makes provenance ambiguous.
- Deleting imported fills, cash movements, or transfers.
- Auth of any kind. §6 replaces it with a structural boundary.
- Multi-leg P&L (`Direction.SPREAD`). Unreachable by construction — see §4.

---

## 9. Gaps this design creates

| Gap | Detail |
|---|---|
| Manual fills are not split-adjusted if wholly owned by a manual trade | Pre-existing, inherited from `regroup_account`'s documented behaviour. Manual grouping is out of scope, so nothing here makes it reachable — but adding manual grouping later does. |
| The write instance has no health check | The deploy script health-checks the published port only. It should learn to check the write port too, or a broken write instance stays invisible until someone tries to type a fill. |
| `DEADBAND_ENABLE_WRITES` is a footgun if set on the served unit | A single environment variable stands between the tailnet and an unauthenticated write surface. The §7 test pins the default; nothing pins the deployed unit file. Consider asserting it in the deploy script. |
