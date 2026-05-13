/**
 * Allocation routes for team-tracker module.
 * Mounted at /api/modules/team-tracker/allocation/ by the team-tracker server.
 */

const { readTeams, extractBoardId } = require('../../../../shared/server/team-store');
const { getOrgDisplayNames } = require('../../../../shared/server/roster-sync/config');
const { allocationKey } = require('./config');

function isValidBoardId(id) { return /^(\d+|kanban-\d+)$/.test(id); }
function isValidSprintId(id) { return /^(\d+|kanban-\d+)$/.test(id); }
const REFRESH_COOLDOWN_MS = 60_000;

module.exports = function registerAllocationRoutes(router, context) {
  const { storage, requireAdmin } = context;
  const { readFromStorage, writeToStorage } = storage;

  const DEMO_MODE = process.env.DEMO_MODE === 'true';

  const { JIRA_HOST, jiraRequest } = require('../../../../shared/server/jira');

  const { createJiraClient } = require('./jira-client');
  const jiraClient = createJiraClient({ jiraRequest, jiraHost: JIRA_HOST });

  const { performRefresh } = require('./orchestration');

  // Storage helpers — all allocation data under allocation/ prefix
  function allocRead(key) { return readFromStorage(allocationKey(key)); }
  function allocWrite(key, data) { writeToStorage(allocationKey(key), data); }

  // ─── Refresh state (in-memory) ───

  const refreshState = {
    running: false,
    startedAt: null,
    completedAt: null,
    lastResult: null
  };

  // ─── Refresh routes ───

  router.post('/allocation/refresh', requireAdmin, async function(req, res) {
    if (DEMO_MODE) {
      return res.json({ status: 'skipped', message: 'Refresh disabled in demo mode' });
    }
    if (refreshState.running) {
      return res.json({ status: 'already_running' });
    }

    // Cooldown check
    if (refreshState.completedAt) {
      const elapsed = Date.now() - new Date(refreshState.completedAt).getTime();
      if (elapsed < REFRESH_COOLDOWN_MS) {
        const retryAfter = Math.ceil((REFRESH_COOLDOWN_MS - elapsed) / 1000);
        return res.json({ status: 'cooldown', retryAfter });
      }
    }

    const teamId = req.query.teamId || req.body.teamId;
    const hardRefresh = req.body.hardRefresh || false;

    refreshState.running = true;
    refreshState.startedAt = new Date().toISOString();
    res.json({ status: 'started' });

    setImmediate(async function() {
      try {
        // Read all teams from team-store
        const teamData = readTeams(storage);
        let teams = Object.values(teamData.teams || {});

        // Filter to single team if requested
        if (teamId) {
          teams = teams.filter(t => t.id === teamId);
          if (teams.length === 0) {
            const completedAt = new Date().toISOString();
            refreshState.lastResult = { status: 'error', message: `Team ${teamId} not found`, completedAt };
            refreshState.completedAt = completedAt;
            refreshState.running = false;
            return;
          }
        }

        // Backfill boardId for boards saved before extraction was added
        for (const t of teams) {
          if (Array.isArray(t.boards)) {
            t.boards = t.boards.map(b => b.boardId != null ? b : { ...b, boardId: extractBoardId(b.url) });
          }
        }
        // Filter to teams with at least one board with a boardId
        teams = teams.filter(t => (t.boards || []).some(b => b.boardId));

        console.log(`\n[allocation] Starting refresh: ${teams.length} teams with allocation boards`);

        const result = await performRefresh({
          teams,
          hardRefresh,
          fetchSprints: jiraClient.fetchSprints,
          fetchSprintIssues: jiraClient.fetchSprintIssues,
          fetchBoardConfiguration: jiraClient.fetchBoardConfiguration,
          fetchFilterJql: jiraClient.fetchFilterJql,
          fetchIssuesByJql: jiraClient.fetchIssuesByJql,
          fetchBoardType: jiraClient.fetchBoardType,
          readStorage: allocRead,
          writeStorage: allocWrite
        });

        const completedAt = new Date().toISOString();
        refreshState.lastResult = {
          status: 'success',
          message: `Processed ${result.teamCount} teams`,
          completedAt
        };
        refreshState.completedAt = completedAt;
        console.log(`[allocation] Refresh complete: ${result.teamCount} teams processed`);
      } catch (error) {
        console.error('[allocation] Background refresh error:', error);
        const completedAt = new Date().toISOString();
        refreshState.lastResult = { status: 'error', message: 'Refresh failed', completedAt };
        refreshState.completedAt = completedAt;
      } finally {
        refreshState.running = false;
      }
    });
  });

  router.get('/allocation/refresh/status', function(_req, res) {
    const sanitized = { ...refreshState };
    if (sanitized.lastResult) {
      sanitized.lastResult = {
        ...sanitized.lastResult,
        message: sanitized.lastResult.status === 'error'
          ? 'Refresh failed'
          : sanitized.lastResult.message
      };
    }
    res.json(sanitized);
  });

  // ─── Summary routes ───

  router.get('/allocation/team/:teamId/summary', function(req, res) {
    try {
      const { teamId } = req.params;
      const data = allocRead(`summaries/team-${teamId}.json`);
      if (!data) {
        return res.json({ lastUpdated: null, totalPoints: 0, boardCount: 0, buckets: {} });
      }
      res.json(data);
    } catch (error) {
      console.error('[allocation] Read team summary error:', error);
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  router.get('/allocation/org/:orgKey/summary', function(req, res) {
    try {
      const orgParam = req.params.orgKey;
      // Try direct lookup first (orgParam is already an orgKey)
      let data = allocRead(`summaries/org-${orgParam}.json`);
      // If not found, resolve display name to orgKey
      if (!data) {
        const displayNames = getOrgDisplayNames(storage);
        const resolvedKey = Object.entries(displayNames).find(([, name]) => name === orgParam)?.[0];
        if (resolvedKey) {
          data = allocRead(`summaries/org-${resolvedKey}.json`);
        }
      }
      if (!data) {
        return res.json({ lastUpdated: null, totalPoints: 0, teamCount: 0, boardCount: 0, buckets: {} });
      }
      res.json(data);
    } catch (error) {
      console.error('[allocation] Read org summary error:', error);
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  router.get('/allocation/global/summary', function(_req, res) {
    try {
      const data = allocRead('summaries/global.json');
      if (!data) {
        return res.json({ lastUpdated: null, totalPoints: 0, teamCount: 0, boardCount: 0, buckets: {} });
      }
      res.json(data);
    } catch (error) {
      console.error('[allocation] Read global summary error:', error);
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  // ─── Sprint data routes ───

  router.get('/allocation/board/:boardId/sprints', function(req, res) {
    try {
      const { boardId } = req.params;
      if (!isValidBoardId(boardId)) {
        return res.status(400).json({ error: 'Invalid request parameter' });
      }

      // If sprintFilter query param, try filter-specific index first
      const sprintFilter = req.query.sprintFilter;
      if (sprintFilter) {
        const filterKey = sprintFilter.trim().toLowerCase().replace(/\s+/g, '-');
        const filtered = allocRead(`sprints/board-${boardId}-${filterKey}.json`);
        if (filtered) return res.json(filtered);
      }

      const data = allocRead(`sprints/board-${boardId}.json`);
      if (!data) {
        return res.json({ sprints: [] });
      }
      res.json(data);
    } catch (error) {
      console.error('[allocation] Read sprints error:', error);
      res.status(500).json({ error: 'Internal server error' });
    }
  });

  router.get('/allocation/sprints/:sprintId/issues', function(req, res) {
    try {
      const { sprintId } = req.params;
      if (!isValidSprintId(sprintId)) {
        return res.status(400).json({ error: 'Invalid request parameter' });
      }
      const data = allocRead(`sprints/${sprintId}.json`);
      if (!data) {
        return res.status(404).json({
          error: 'Sprint data not found. Please refresh to fetch data from Jira.'
        });
      }
      res.json(data);
    } catch (error) {
      console.error('[allocation] Read sprint issues error:', error);
      res.status(500).json({ error: 'Internal server error' });
    }
  });

};
