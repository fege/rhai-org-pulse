# Health Metrics Cleanup Plan (Step 1.5)

**Goal:** Clean up `server/health-metrics/` to follow the same standards established by Steps 1.1-1.4 of platform modularization. Fix hard constraint violations (raw filesystem access, direct `DATA_DIR` usage) and align role/scope registration with the module context pattern.

**Scope:** No route changes, no API contract changes, no frontend changes. Backend-only refactoring.

---

## Phase 1: Add `getFileMtime` to storage.js and demo-storage.js

### File: `shared/server/storage.js`

**Add `getFileMtime(key)`** — returns the modification time of a storage file without reading its contents. Useful for cache invalidation polling. Internally delegates to `isPathSafe` for path-traversal safety (no duplicated logic).

```js
/**
 * Get the modification time of a storage file without reading it.
 * @param {string} key - S3-style key
 * @returns {number|null} mtime in milliseconds, or null if file doesn't exist or path is unsafe
 */
function getFileMtime(key) {
  const filePath = path.resolve(DATA_DIR, key);
  if (!isPathSafe(filePath)) {
    console.error(`[storage] Blocked path traversal attempt: ${key}`);
    return null;
  }
  try {
    return fs.statSync(filePath).mtimeMs;
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    throw err;
  }
}
```

**Update `module.exports`** to include `getFileMtime`.

### File: `shared/server/demo-storage.js`

Add a matching `getFileMtime` resolving against `FIXTURES_DIR`. In demo mode, fixture file mtimes are static (set at clone/build time), so the mtime polling in `routes.js` will fire but never detect a change — the user type cache is built once at startup and remains stable. This is correct demo behavior (no roster sync runs in demo mode, so there's nothing to detect).

```js
/**
 * Get the modification time of a fixture file without reading it.
 * In demo mode, mtimes are static — polling detects no changes (by design).
 * @param {string} key - S3-style key
 * @returns {number|null} mtime in milliseconds, or null if not found
 */
function getFileMtime(key) {
  const filePath = path.resolve(FIXTURES_DIR, key);
  if (!isPathSafe(filePath)) {
    console.error(`[demo-storage] Blocked path traversal attempt: ${key}`);
    return null;
  }
  try {
    return fs.statSync(filePath).mtimeMs;
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    throw err;
  }
}
```

**Update `module.exports`** to include `getFileMtime`.

### File: `shared/API.md`

Update the Server Exports table — add `getFileMtime` to the `storage` row and the `demoStorage` row:

```
| `storage` | `{ readFromStorage, writeToStorage, writeToStorageAtomic, listStorageFiles, deleteStorageDirectory, deleteFromStorage, getFileMtime }` — filesystem-backed JSON storage. `getFileMtime(key)` returns file mtime in ms without reading (for cache invalidation). |
| `demoStorage` | `{ readFromStorage, writeToStorage, writeToStorageAtomic, listStorageFiles, deleteStorageDirectory, deleteFromStorage, getFileMtime }` — fixture-backed read-only storage for demo mode |
```

### Backward Compatibility

- **Additive only** — no existing signatures change.
- `DATA_DIR` export remains unchanged (other consumers may use it; removing it is a separate concern).

---

## Phase 2: Update event-store.js constructor

### File: `server/health-metrics/event-store.js`

**Change the constructor signature** from `createEventStore(dataDir)` to `createEventStore(eventsDir)` — it now receives the fully resolved events directory path directly instead of constructing it from `dataDir`.

Before:
```js
function createEventStore(dataDir) {
  const eventsDir = path.join(dataDir, 'health-metrics', 'events');
```

After:
```js
function createEventStore(eventsDir) {
```

The rest of the file is unchanged — it continues to use raw `fs` for JSONL operations, which is acceptable since JSONL doesn't fit the JSON storage abstraction.

### File: `server/health-metrics/__tests__/event-store.test.js`

No test changes needed. Tests call store methods (append, readMonth, etc.) and don't depend on the internal directory structure. With the new signature, `tmpDir` is used directly as the events directory instead of deriving a nested subdirectory.

### Backward Compatibility

- `createEventStore` signature changes from `(dataDir)` to `(eventsDir)`.
- Only one caller exists: the `createHealthMetricsRouter` function in `routes.js`. Updated in Phase 3.

---

## Phase 3: Update routes.js to remove all raw filesystem access

### File: `server/health-metrics/routes.js`

**Remove `DATA_DIR` import** (in `createHealthMetricsRouter`, after destructuring context):
```js
// DELETE: const { DATA_DIR } = require('../../shared/server/storage');
```

**Update event store creation.** `createHealthMetricsRouter` receives the events directory as an option from `dev-server.js` (see Phase 4), eliminating the `DATA_DIR` import entirely:

```js
function createHealthMetricsRouter(context, { eventsDir } = {}) {
  const { storage, requireAdmin, requireScope, roleStore } = context;
  const { readFromStorage, writeToStorage, getFileMtime, listStorageFiles } = storage;

  const router = express.Router();
  const DEMO_MODE = process.env.DEMO_MODE === 'true';
  const eventStore = DEMO_MODE ? null : createEventStore(eventsDir);
```

In demo mode, `eventStore` is `null` — all event store operations are already guarded by `DEMO_MODE` checks (the `/track` route returns early, pruning is skipped, dashboard raw-event reads are skipped). This is consistent with the existing code where event logging is a no-op in demo mode.

**Replace mtime polling** (in `createHealthMetricsRouter`, the `setInterval` block). Replace raw `fs.statSync` + `path.join(DATA_DIR, ...)` with `getFileMtime` from the storage context:

Before:
```js
const registryCheckInterval = setInterval(() => {
  try {
    const fs = require('fs');
    const path = require('path');
    const registryPath = path.join(DATA_DIR, 'team-data', 'registry.json');
    const stat = fs.statSync(registryPath);
    if (stat.mtimeMs > registryMtime) {
      registryMtime = stat.mtimeMs;
      rebuildUserTypeCache();
    }
  } catch {
    // registry file may not exist yet
  }
}, 60_000);
```

After:
```js
const registryCheckInterval = setInterval(() => {
  const mtime = getFileMtime('team-data/registry.json');
  if (mtime && mtime > registryMtime) {
    registryMtime = mtime;
    rebuildUserTypeCache();
  }
}, 60_000);
```

**Update `collectAggregates`** (the `storedFiles` line in `collectAggregates` function). Destructure `listStorageFiles` at the top so it's consistent:

Change:
```js
const storedFiles = (storage.listStorageFiles?.('health-metrics/aggregates') || []);
```
to:
```js
const storedFiles = (listStorageFiles?.('health-metrics/aggregates') || []);
```

**Add cross-module dependency comments** at the two `readFromStorage` calls:

In `rebuildUserTypeCache`:
```js
// Cross-module read: team-tracker exports team-data/registry.json (see module.json > export.files)
const registry = readFromStorage('team-data/registry.json');
```

In the `/field-definitions` route handler:
```js
// Cross-module read: team-tracker exports team-data/field-definitions.json (see module.json > export.files)
const fieldDefs = readFromStorage('team-data/field-definitions.json');
```

### Backward Compatibility

- No API changes — all routes, request/response shapes, and behavior are identical.
- `createHealthMetricsRouter` signature changes from `(context)` to `(context, { eventsDir })`. The only caller is `dev-server.js`, updated in Phase 4.

---

## Phase 4: Organize role/scope registrations and pass `eventsDir`

### Current state

Health-metrics roles and scopes are registered inline in `dev-server.js` (in the "Registries" section, between platform scope registration and `apiTokens.init`), with an ad-hoc comment:

```js
// Health-metrics roles and scopes (health-metrics is NOT a module — see plan A.1)
roleRegistry.register('usage-metrics-viewer', { ... module: 'health-metrics' });
scopeRegistry.register('health-metrics:read', { ... module: 'health-metrics' });
scopeRegistry.register('health-metrics:write', { ... module: 'health-metrics' });
```

### Target state

Keep the inline registrations (health-metrics is platform code, not a module), but organize them under a clear section header. Update the health-metrics mounting section to compute and pass `eventsDir`.

### File: `server/dev-server.js`

**Reorganize the registrations section.** Replace the ad-hoc comment with a clear section header that separates platform scopes from platform subsystem scopes:

Before:
```js
// Platform scopes
const platformScopes = [ ... ];
for (const s of platformScopes) {
  scopeRegistry.register(s.key, { ...s, module: 'platform' });
}

// Health-metrics roles and scopes (health-metrics is NOT a module — see plan A.1)
roleRegistry.register('usage-metrics-viewer', { ... });
scopeRegistry.register('health-metrics:read', { ... });
scopeRegistry.register('health-metrics:write', { ... });
```

After:
```js
// Platform scopes
const platformScopes = [ ... ];
for (const s of platformScopes) {
  scopeRegistry.register(s.key, { ...s, module: 'platform' });
}

// ─── Platform subsystem registrations (health-metrics) ───
// Health-metrics is a platform concern, not a module. Its roles and scopes
// are registered here alongside other platform registrations.
roleRegistry.register('usage-metrics-viewer', {
  label: 'Usage Metrics Viewer',
  description: 'Can view health/usage metrics dashboards',
  module: 'health-metrics'
});
scopeRegistry.register('health-metrics:read', { label: 'Health Metrics (Read)', description: 'Read health metrics data', category: 'Health Metrics', module: 'health-metrics' });
scopeRegistry.register('health-metrics:write', { label: 'Health Metrics (Write)', description: 'Mutate health metrics data', category: 'Health Metrics', module: 'health-metrics' });
```

**Update the health-metrics mounting section** to compute `eventsDir` and pass it to the router:

Before:
```js
// ─── Health Metrics (core feature, not a module) ───
const { createHealthMetricsRouter } = require('./health-metrics/routes');
app.use('/api/health-metrics', createHealthMetricsRouter(coreServices));
```

After:
```js
// ─── Health Metrics (core feature, not a module) ───
const path = require('path');
const { createHealthMetricsRouter } = require('./health-metrics/routes');
const hmDataRoot = storageModule.DATA_DIR || storageModule.FIXTURES_DIR;
const eventsDir = path.join(hmDataRoot, 'health-metrics', 'events');
app.use('/api/health-metrics', createHealthMetricsRouter(coreServices, { eventsDir }));
```

**Notes:**
- `path` is not currently imported in `dev-server.js` — add the `require('path')` as shown. Alternatively, hoist it to the top-level imports for consistency.
- `storageModule` is already in scope (line 23) and exports `DATA_DIR` or `FIXTURES_DIR` depending on demo mode.
- `createHealthMetricsRouter` continues to receive `coreServices` (not a ModuleContext) — health-metrics stays explicitly platform code.

In demo mode, `eventsDir` points to `fixtures/health-metrics/events/`. The `DEMO_MODE` guards in routes.js prevent any actual event store writes, and the event store is set to `null` in demo mode (see Phase 3).

### File: `server/health-metrics/routes.js`

No role/scope registration changes — they stay in `dev-server.js`. The only change is the signature update to accept `{ eventsDir }` (already covered in Phase 3).

### File: `server/health-metrics/__tests__/routes.test.js`

Existing tests are pure logic tests (no Express mounting). They don't test `createHealthMetricsRouter` directly, so no changes needed.

### Backward Compatibility

- Role/scope registrations are unchanged — same values, same location, just better organized with a section header.
- `createHealthMetricsRouter` signature changes from `(coreServices)` to `(coreServices, { eventsDir })`. The only caller is `dev-server.js`, updated in the same phase.

---

## Phase 5: Verify and test

### Automated testing

1. **Run existing tests**: `npm test` — all existing event-store and routes tests must pass.
2. **Run linting**: `npm run lint` — CI rejects lint failures.
3. **Run module validation**: `npm run validate:modules` — health-metrics is not a module, but ensure no regressions.

### Manual testing

1. **Start dev server**: `npm run dev:full`
2. **Verify role/scope registration**: `GET /api/roles/available` — should include `usage-metrics-viewer`. `GET /api/token-scopes` — should include `health-metrics:read` and `health-metrics:write`.
3. **Verify health metrics tracking**: Navigate around the app, then `GET /api/health-metrics/dashboard` as admin — should show page views.
4. **Verify mtime polling**: Trigger a roster sync, wait 60s, verify user type cache rebuilds (check server logs for cache rebuild).
5. **Demo mode**: `DEMO_MODE=true npm run dev:full` — app should start without errors, no event store writes attempted.

### Container smoke tests (recommended before merge)

Run `make smoke-test` to build production containers and verify the app starts correctly. This catches issues with storage path resolution in the containerized environment that local dev testing may miss. Smoke tests run in `DEMO_MODE=true`, so they validate the demo-storage code path specifically.

---

## Summary of changes by file

| File | Changes |
|------|---------|
| `shared/server/storage.js` | Add `getFileMtime(key)`, update exports |
| `shared/server/demo-storage.js` | Add matching `getFileMtime(key)` against `FIXTURES_DIR` |
| `shared/API.md` | Document `getFileMtime` in storage and demoStorage rows |
| `server/health-metrics/event-store.js` | Change constructor from `(dataDir)` to `(eventsDir)` — remove internal path construction |
| `server/health-metrics/routes.js` | Remove `DATA_DIR` import, use `getFileMtime` from storage context, accept `{ eventsDir }` option, skip event store in demo mode, add cross-module comments |
| `server/dev-server.js` | Organize health-metrics registrations under section header, compute `eventsDir`, pass to router |

## Architectural decision: Why NOT `getStoragePath`

The original plan proposed adding `getStoragePath(key)` to `storage.js` as a public API for resolving storage keys to raw filesystem paths. This was removed because:

1. **It weakens the abstraction it lives in.** Once `getStoragePath` is a blessed, documented API, it invites module developers to bypass `readFromStorage`/`writeToStorage` for convenience. This undermines Hard Constraint #2.
2. **The problem is specific, not general.** Only health-metrics needs a raw path (for JSONL append), and it's a platform concern, not a module. General modules should never need this.
3. **The alternative is simpler.** `dev-server.js` (the orchestrator) already has access to `DATA_DIR`/`FIXTURES_DIR` through `storageModule`. Computing the events directory path there and passing it as a constructor argument keeps the escape hatch in the orchestrator, not in the shared API.

## Risk assessment

- **Low risk**: All changes are internal refactoring. No API contracts change. No data format changes. No frontend changes.
- **One-way door**: The `createEventStore` signature change is breaking for its single caller, but both sides are updated atomically.
- **Timing risk**: None. Role/scope registrations stay in their current location in `dev-server.js` — no ordering changes.
- **Demo mode**: Explicitly handled — event store is `null` in demo mode, `getFileMtime` in demo-storage returns static mtimes (no false cache rebuilds).
