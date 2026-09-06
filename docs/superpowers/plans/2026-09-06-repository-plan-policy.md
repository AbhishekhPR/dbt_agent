# Repository Selection Plan Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every GitHub-authorized repository while applying Free, Starter, and Pro repository limits as a separate server-derived policy that remains transactionally enforced.

**Architecture:** Extend `GET /api/onboarding/repositories` with independent `authorization` and `policy` objects while leaving its complete `repositories` list unchanged. The React onboarding flow stores the three-part snapshot atomically, disables only unconnected repositories when the finite Relium limit is reached, and refreshes the entire snapshot in place. The existing PostgreSQL tenant lock/count/insert guard remains authoritative.

**Tech Stack:** Python 3.10+, Starlette, psycopg, `unittest`; React 19, Vite, Vitest, Testing Library.

---

### Task 1: Add the backend repository-policy response contract

**Files:**
- Modify: `test_onboarding_end_to_end.py`
- Modify: `agent/api/onboarding_repository_routes.py`

- [ ] **Step 1: Write failing route assertions**

Require the complete repository set plus independent authorization and policy
fields in the repository-list response:

```python
body = listing.json()
self.assertEqual(
    {r["repository_id"] for r in body["repositories"]},
    {REPO_ID, OTHER_REPO_ID},
)
self.assertEqual(body["authorization"], {
    "authorized_count": 2,
    "github_installations": [{
        "installation_id": INSTALLATION,
        "account_login": "acme-analytics",
        "account_type": "Organization",
    }],
})
self.assertEqual(body["policy"], {
    "plan": "free",
    "repository_limit": 1,
    "connected_repository_count": 0,
})
```

Add route cases with active Starter and Pro billing rows and require limits `3`
and `None`, without changing the returned repository set.

- [ ] **Step 2: Run the route test and verify RED**

Run `python -m unittest test_onboarding_end_to_end -v` with the repository's
existing `RELIUM_TEST_POSTGRES_DSN`. Expected: failures because
`authorization` and `policy` are absent.

- [ ] **Step 3: Implement the response snapshot**

In `create_onboarding_repository_routes`, import `get_workspace_plan` alongside
`get_workspace_entitlements`. Return the current complete repository payload
plus:

```python
"authorization": {
    "authorized_count": len(repositories),
    "github_installations": [
        {
            "installation_id": row["github_installation_id"],
            "account_login": row["github_account_login"],
            "account_type": row["github_account_type"],
        }
        for row in store.tenant_github_installations(principal.tenant_id)
        if row.get("status") == "active"
    ],
},
"policy": {
    "plan": get_workspace_plan(
        store, principal.tenant_id, billing_settings),
    "repository_limit": _entitlements(
        store, principal).repository_limit,
    "connected_repository_count":
        store.count_tenant_repositories(principal.tenant_id),
},
```

Do not filter or slice `repositories` using any policy value.

- [ ] **Step 4: Run the route test and verify GREEN**

Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Commit the backend contract**

```powershell
git add agent/api/onboarding_repository_routes.py test_onboarding_end_to_end.py
git commit -m "feat: expose repository plan policy"
```

### Task 2: Pin the transactional entitlement matrix

**Files:**
- Modify: `test_plan_entitlements.py`
- Modify only if a regression fails: `agent/postgres_lifecycle_store.py`

- [ ] **Step 1: Add Starter boundary tests**

```python
def test_starter_allows_three_repositories(self):
    store = self._store(existing={100, 200})
    self.assertIsNotNone(self._select(store, 300, 3))

def test_starter_refuses_a_fourth_repository(self):
    from agent.postgres_lifecycle_store import TenantRepositoryLimitReached
    store = self._store(existing={100, 200, 300})
    with self.assertRaises(TenantRepositoryLimitReached):
        self._select(store, 400, 3)

def test_starter_can_reselect_at_its_limit(self):
    store = self._store(existing={100, 200, 300})
    self.assertIsNotNone(self._select(store, 200, 3))
```

- [ ] **Step 2: Run the focused tests**

Run `python -m unittest test_plan_entitlements.RepositoryLimitStoreTests -v`.
They may already pass because this task preserves current behavior. Retain them
as regression coverage and make no production change if they pass.

- [ ] **Step 3: Commit the enforcement coverage**

```powershell
git add test_plan_entitlements.py
git commit -m "test: pin repository limit enforcement"
```

### Task 3: Translate the policy snapshot in the dashboard

**Files:**
- Create worktree: `C:/Users/Abhishekh/.worktrees/relium-app/fix-repository-plan-policy`
- Modify: `src/onboarding/onboardingApi.js`
- Create: `src/onboarding/onboardingApi.test.js`

- [ ] **Step 1: Create a frontend worktree from current `origin/main`**

Fetch `origin/main`, create branch `fix/repository-plan-policy`, install package
dependencies, and run the existing onboarding tests as the clean baseline.

- [ ] **Step 2: Write a failing API translation test**

```javascript
expect(await listAuthorizedRepositories()).toEqual({
  repositories: [expect.objectContaining({ id: 11, selected: false })],
  authorization: {
    authorizedCount: 1,
    githubInstallations: [{
      installationId: 42,
      accountLogin: 'acme',
      accountType: 'Organization',
    }],
  },
  policy: {
    plan: 'starter',
    repositoryLimit: 3,
    connectedRepositoryCount: 1,
  },
})
```

- [ ] **Step 3: Run and verify RED**

Run `npm run test:unit -- src/onboarding/onboardingApi.test.js`. Expected: the
function returns an array instead of the snapshot.

- [ ] **Step 4: Implement the translation**

Map the response into `{repositories, authorization, policy}`. Preserve
`null` as the unlimited repository limit, map installation fields to camelCase,
and never infer a plan when `policy.plan` is absent.

- [ ] **Step 5: Verify GREEN and commit**

Run the focused test, then commit `onboardingApi.js` and its test with message
`feat: read repository policy snapshot`.

### Task 4: Render authorization and plan policy separately

**Files:**
- Modify: `src/onboarding/Onboarding.jsx`
- Create: `src/onboarding/Onboarding.repository-policy.test.jsx`

- [ ] **Step 1: Write the failing UI matrix**

Export `RepositoryStep` for direct behavior tests. Use policy fixtures:

```javascript
const freeAtLimit = {
  plan: 'free', repositoryLimit: 1, connectedRepositoryCount: 1,
}
const starter = {
  plan: 'starter', repositoryLimit: 3, connectedRepositoryCount: 1,
}
const pro = {
  plan: 'pro', repositoryLimit: null, connectedRepositoryCount: 8,
}
```

Assert that Free renders every authorized row but disables unconnected rows at
the limit; Starter shows and enables capacity up to three; Pro shows and enables
all eligible rows with no count restriction; and Starter plus one authorized
repository shows the exact GitHub authorization sentence and management action,
without Free copy.

- [ ] **Step 2: Run and verify RED**

Run `npm run test:unit -- src/onboarding/Onboarding.repository-policy.test.jsx`.
Expected: policy props, plan-disabled rows, and management links do not exist.

- [ ] **Step 3: Implement policy-aware rows and copy**

Compute:

```javascript
const limit = policy?.repositoryLimit
const connected = policy?.connectedRepositoryCount ?? 0
const atLimit = Number.isInteger(limit) && connected >= limit
const planName = policy?.plan ? PLANS[policy.plan]?.name : null
const blockedByPlan = (repo) => atLimit && !repo.selected
```

Keep every repository in the map and use
`disabled={repo.dbtDetected !== true || blockedByPlan(repo)}`. Render finite plan
copy only for Free and Starter, render clear upgrade text at the limit, and
render no repository-count restriction for Pro.

Derive the management URL only from verified installation metadata:

```javascript
export function githubInstallationSettingsUrl(installation) {
  const id = Number(installation?.installationId)
  if (!Number.isSafeInteger(id) || id <= 0) return null
  if (installation.accountType === 'Organization'
      && installation.accountLogin) {
    return `https://github.com/organizations/${encodeURIComponent(
      installation.accountLogin)}/settings/installations/${id}`
  }
  if (installation.accountType === 'User') {
    return `https://github.com/settings/installations/${id}`
  }
  return null
}
```

- [ ] **Step 4: Verify GREEN and commit**

Run the focused UI test and commit with message
`fix: separate repository authorization from plan limits`.

### Task 5: Refresh repositories and policy atomically

**Files:**
- Modify: `src/onboarding/Onboarding.jsx`
- Modify: `src/onboarding/Onboarding.repository-policy.test.jsx`
- Modify: existing onboarding fixtures returning repository responses

- [ ] **Step 1: Write a failing Free-to-Starter refresh test**

Mount `Onboarding` once. Return a Free-at-limit snapshot on the first repository
GET and a Starter snapshot on the second. Click `Refresh repositories` and
assert the previously disabled repository becomes enabled without unmounting.

- [ ] **Step 2: Run and verify RED**

Run the focused policy test. Expected: no refresh action or atomic snapshot
state exists.

- [ ] **Step 3: Implement atomic snapshot state**

Use one state value for repositories, authorization, and policy. Create a stable
`loadRepositorySnapshot` callback, use it after installation reconciliation,
on repository-step entry, and from a visible `Refresh repositories` action.
Replace the snapshot in one state update.

- [ ] **Step 4: Update fixtures and verify GREEN**

Update all mocked repository-list responses to include explicit authorization
and policy. Run the policy, new-user integration, reconciliation, and CI tests.

- [ ] **Step 5: Commit**

Commit all frontend refresh and fixture changes with message
`fix: refresh repository policy in place`.

### Task 6: Full verification and PRs

**Files:**
- Verify all modified backend and frontend files

- [ ] **Step 1: Run complete backend verification**

With the existing local PostgreSQL test DSN exported as
`RELIUM_TEST_POSTGRES_DSN`, run:

```powershell
python -m unittest discover -s . -p "test_*.py" -v
python -m compileall agent
git diff --check origin/main...HEAD
detect-secrets scan --baseline .secrets.baseline --all-files --exclude-files '^\.git/'
```

- [ ] **Step 2: Run complete frontend verification**

Run `npm test`, a production `npm run build` with synthetic required public
variables, `npm run scan:bundle`, and `git diff --check origin/main...HEAD`.

- [ ] **Step 3: Review scope**

Confirm the backend diff contains only the design/plan, response contract, and
tests. Confirm the frontend diff contains only onboarding API/UI/tests. Verify
that no credentials or production identifiers were introduced.

- [ ] **Step 4: Push and open two PRs**

Push `fix/repository-plan-policy` in each repository and create separate backend
and dashboard PRs against `main`. Inspect their file lists and check status. Do
not merge or deploy either PR.
