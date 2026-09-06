/* ProofFirst workspace — vanilla JS, no build step (matches existing deploy model) */
(() => {
  "use strict";

  // ---------------------------------------------------------------
  // Tiny helpers
  // ---------------------------------------------------------------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const el = (tag, attrs = {}, children = []) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) n.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c === null || c === undefined) continue;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return n;
  };
  const esc = (s) => (s ?? "").toString().replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtPct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
  const fmtAgo = (ts) => {
    if (!ts) return "—";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 5) return "just now";
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  };
  const confColor = (v) => (v === null || v === undefined ? "var(--text-tertiary)" : v >= 0.75 ? "var(--green-2)" : v >= 0.45 ? "var(--amber-2)" : "var(--red-2)");

  // One shared visual vocabulary for every agent in the pipeline — the same
  // dot color/icon shows up in the timeline (slide-over panel) and in the
  // Agents grid, so it reads as one consistent language across the app.
  const AGENT_META = {
    ResearchAgent: { icon: "search" },
    SkepticAgent: { icon: "search" },
    EvidenceSynthesisAgent: { icon: "check" },
    DecisionAgent: { icon: "check" },
    ActionDecisionAgent: { icon: "check" },
    PreflightVerifierAgent: { icon: "check" },
    ActionExecutor: { icon: "check" },
    PostActionVerificationAgent: { icon: "check" },
    OutreachAgent: { icon: "search" },
    LiveActionGuard: { icon: "check" },
    LiveCallAgent: { icon: "check" },
  };
  function agentDot(name) {
    return el("span", { class: `agent-dot agent-dot-${name}` });
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail || `Request failed (${res.status})`);
    }
    return res.json();
  }

  function toast(msg) {
    const wrap = $("#toastWrap");
    const t = el("div", { class: "toast" }, msg);
    wrap.appendChild(t);
    setTimeout(() => t.remove(), 3200);
  }

  // ---------------------------------------------------------------
  // Global state
  // ---------------------------------------------------------------
  const State = {
    leadsBasic: [],       // from /api/leads
    overview: [],         // from /api/overview
    mockMode: false,
    route: "home",
    leadsView: "all",     // saved view id
    leadsSearch: "",
    sortKey: "updated",
    sortDir: "desc",
    selectedLeadId: null,
    evidenceLeadId: null,
    evidenceClaimId: null,
    loading: {},
    inFlightRuns: new Set(),      // lead_ids with a POST /run in progress — hard guard against double-fire
    recentlyUpdated: new Set(),   // lead_ids to flash on next table render
    onboardingDismissed: false,
  };

  const SAVED_VIEWS = [
    { id: "all", label: "All Leads", filter: () => true },
    { id: "high", label: "High Confidence", filter: (r) => (r.confidence ?? 0) >= 0.75 },
    { id: "review", label: "Needs Review", filter: (r) => r.status === "reviewed" || r.action_status === "PENDING_APPROVAL" },
    { id: "contradicted", label: "Contradicted", filter: (r) => r.status === "blocked" },
    { id: "pending", label: "Pending Actions", filter: (r) => r.action_status === "PENDING_APPROVAL" },
    { id: "verified", label: "Recently Verified", filter: (r) => r.status === "verified" },
    { id: "not_started", label: "Not Started", filter: (r) => r.status === "not_started" },
  ];

  // ---------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------
  async function loadOverview({ silent } = {}) {
    if (!silent) State.loading.overview = true;
    try {
      const [leadsRes, overviewRes] = await Promise.all([api("/api/leads"), api("/api/overview")]);
      State.leadsBasic = leadsRes.leads;
      State.mockMode = leadsRes.mock_mode;
      State.overview = overviewRes.leads;
      updateSidebarCounts();
      updateStatusFooter();
    } finally {
      State.loading.overview = false;
    }
  }

  function updateSidebarCounts() {
    $("#navLeadsCount").textContent = State.overview.length ? State.overview.length : "";
    const pending = State.overview.filter((r) => r.action_status === "PENDING_APPROVAL").length;
    $("#navActionsCount").textContent = pending ? pending : "";
  }

  function updateStatusFooter() {
    const dot = $("#statusDot");
    const label = $("#statusLabel");
    if (State.mockMode) {
      dot.classList.add("mock");
      label.textContent = "Running in mock mode";
    } else {
      dot.classList.remove("mock");
      label.textContent = "System operational";
    }
  }

  // ---------------------------------------------------------------
  // Router
  // ---------------------------------------------------------------
  const ROUTE_TITLES = {
    home: "Home", inbox: "Inbox", leads: "Leads", companies: "Companies",
    evidence: "Evidence", actions: "Actions", agents: "Agents", activity: "Activity",
    reports: "Reports", evaluation: "Evaluation", settings: "Settings",
  };

  function navigate(route, opts = {}) {
    State.route = route;
    Object.assign(State, opts);
    location.hash = `#${route}`;
    render();
  }

  window.addEventListener("hashchange", () => {
    const route = (location.hash || "#home").slice(1).split("?")[0] || "home";
    State.route = ROUTE_TITLES[route] ? route : "home";
    render();
  });

  // ---------------------------------------------------------------
  // Render dispatch
  // ---------------------------------------------------------------
  function render() {
    $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.route === State.route));
    $("#topbarTitle").textContent = ROUTE_TITLES[State.route] || "Home";
    $("#topbarCrumb").classList.add("hidden");

    const view = $("#view");
    view.innerHTML = "";
    view.scrollTop = 0;

    const renderers = {
      home: renderHome, inbox: renderInbox, leads: renderLeads, companies: renderLeads,
      evidence: renderEvidenceHub, actions: renderActions, agents: renderAgents,
      activity: renderActivity, reports: renderReports, evaluation: renderEvaluation,
      settings: renderSettings,
    };
    (renderers[State.route] || renderHome)(view);
    applyLiveCallBadges();
  }

  // ---------------------------------------------------------------
  // HOME
  // ---------------------------------------------------------------
  function renderHome(view) {
    view.appendChild(el("h1", { class: "page-title" }, "ProofFirst"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Evidence-first prospecting."));

    if (State.loading.overview && !State.overview.length) {
      view.appendChild(el("div", { class: "section-label" }, "Active Work"));
      const wrap = el("div", { class: "work-list" });
      for (let i = 0; i < 5; i++) wrap.appendChild(el("div", { class: "skeleton skeleton-row" }));
      view.appendChild(wrap);
      return;
    }

    const active = State.overview.filter((r) => r.status !== "not_started")
      .sort((a, b) => (b.last_event?.ts || 0) - (a.last_event?.ts || 0));

    // First-run onboarding: only makes sense before anyone has touched a
    // lead, and only until the person dismisses it once (per session).
    if (!active.length && !State.onboardingDismissed && State.overview.length) {
      view.appendChild(onboardingBanner());
    }

    view.appendChild(el("div", { class: "section-label" }, "Active Work"));

    if (!active.length) {
      view.appendChild(emptyState({
        title: "No active work yet.",
        sub: "Run research on a lead to see it appear here as agents investigate.",
        cta: "Go to Leads",
        onCta: () => navigate("leads"),
      }));
      return;
    }

    const list = el("div", { class: "work-list fade-in" });
    active.slice(0, 12).forEach((r) => {
      const row = el("div", { class: "work-row", "data-lead-id": r.id }, [
        el("div", {}, [
          el("div", { class: "work-lead-name" }, r.name),
          el("div", { class: "work-lead-sub" }, r.vertical),
        ]),
        el("div", { class: "work-meta" }, r.last_event ? r.last_event.agent : "—"),
        confidenceMeter(r.confidence),
        el("div", {}, statusPill(r.status)),
        el("div", { class: "work-time" }, r.last_event ? fmtAgo(r.last_event.ts) : "—"),
      ]);
      row.addEventListener("click", () => openRecordPanel(r.id));
      list.appendChild(row);
    });
    view.appendChild(list);

    view.appendChild(el("div", { class: "section-label" }, "Not started"));
    const rest = State.overview.filter((r) => r.status === "not_started");
    if (!rest.length) {
      view.appendChild(emptyState({ title: "Every lead has been researched.", sub: "There's nothing waiting to be picked up." }));
    } else {
      const list2 = el("div", { class: "work-list" });
      rest.slice(0, 8).forEach((r) => {
        const row = el("div", { class: "work-row", "data-lead-id": r.id }, [
          el("div", {}, [el("div", { class: "work-lead-name" }, r.name), el("div", { class: "work-lead-sub" }, r.vertical)]),
          el("div", { class: "work-meta" }, "—"),
          el("div", {}),
          el("div", {}, statusPill(r.status)),
          el("div", { class: "work-time" }, ""),
        ]);
        row.addEventListener("click", () => openRecordPanel(r.id));
        list2.appendChild(row);
      });
      view.appendChild(list2);
    }
  }

  function confidenceMeter(v) {
    const wrap = el("div", { class: "confidence" });
    const track = el("div", { class: "confidence-track" });
    const fill = el("div", { class: "confidence-fill" });
    const target = v === null || v === undefined ? "0%" : `${Math.round(v * 100)}%`;
    fill.style.background = confColor(v);
    // Fill starts at 0 (set in CSS) and animates to its real value on first
    // render — this is the single most "alive" element on the page, so it
    // shouldn't just appear fully-drawn.
    requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = target; }));
    track.appendChild(fill);
    wrap.appendChild(track);
    wrap.appendChild(el("span", { class: "confidence-num" }, fmtPct(v)));
    return wrap;
  }

  function statusPill(status) {
    const map = {
      not_started: ["pill-neutral", "Not started"],
      reviewed: ["pill-blue", "Investigating"],
      pending: ["pill-amber", "Pending approval"],
      blocked: ["pill-red", "Blocked"],
      verified: ["pill-green", "Verified"],
    };
    const [cls, label] = map[status] || ["pill-neutral", status];
    return el("span", { class: `pill ${cls}` }, [el("span", { class: "pill-dot" }), label]);
  }

  function emptyState({ title, sub, cta, onCta, icon }) {
    const wrap = el("div", { class: "empty-state" });
    wrap.appendChild(el("div", { class: "empty-icon" }, icon || svgIcon("search")));
    wrap.appendChild(el("div", { class: "empty-title" }, title));
    if (sub) wrap.appendChild(el("div", { class: "empty-sub" }, sub));
    if (cta) {
      const btn = el("button", { class: "btn btn-primary btn-sm" }, cta);
      btn.addEventListener("click", onCta);
      wrap.appendChild(btn);
    }
    return wrap;
  }

  function onboardingBanner() {
    const banner = el("div", { class: "onboarding-banner fade-in" }, [
      el("div", {}, [
        el("div", { class: "onboarding-title" }, "Click any lead to run ProofFirst's evidence pipeline."),
        el("div", { class: "onboarding-sub" }, "Research → Skeptic → Decision runs in a couple of seconds and shows up right here."),
      ]),
    ]);
    const dismiss = el("button", { class: "panel-close", title: "Dismiss" }, [
      (() => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "16"); s.setAttribute("height", "16"); s.innerHTML = '<path d="M18 6 6 18M6 6l12 12" fill="none" stroke="currentColor" stroke-width="2"/>'; return s; })(),
    ]);
    dismiss.addEventListener("click", () => { State.onboardingDismissed = true; banner.remove(); });
    banner.appendChild(dismiss);
    return banner;
  }

  // Briefly pulses the row for a lead that just changed state, so an
  // in-place update is noticeable rather than silent. Looks for the row in
  // whatever view is currently on screen — a no-op if it's not rendered.
  function flashRow(leadId) {
    requestAnimationFrame(() => {
      $$(`[data-lead-id="${cssEscape(leadId)}"]`).forEach((row) => {
        row.classList.remove("row-flash");
        void row.offsetWidth; // restart animation if it's already flashing
        row.classList.add("row-flash");
        setTimeout(() => row.classList.remove("row-flash"), 1400);
      });
    });
  }
  function cssEscape(s) { return (s || "").replace(/["\\]/g, "\\$&"); }

  function svgIcon(name) {
    const paths = {
      search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
      check: '<path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/>',
    };
    const wrap = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    wrap.setAttribute("viewBox", "0 0 24 24");
    wrap.setAttribute("width", "28");
    wrap.setAttribute("height", "28");
    wrap.setAttribute("fill", "none");
    wrap.setAttribute("stroke", "currentColor");
    wrap.setAttribute("stroke-width", "1.6");
    wrap.innerHTML = paths[name] || paths.search;
    return wrap;
  }

  // ---------------------------------------------------------------
  // LEADS TABLE
  // ---------------------------------------------------------------
  function renderLeads(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Leads"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Every lead ProofFirst is tracking, with live confidence and decision state."));

    const toolbar = el("div", { class: "table-toolbar" });
    const tabs = el("div", { class: "view-tabs" });
    SAVED_VIEWS.forEach((v) => {
      const tab = el("div", { class: `view-tab ${State.leadsView === v.id ? "active" : ""}` }, v.label);
      tab.addEventListener("click", () => { State.leadsView = v.id; renderLeads(view); });
      tabs.appendChild(tab);
    });
    toolbar.appendChild(tabs);

    const search = el("div", { class: "search-box" });
    search.appendChild(svgSmall("search"));
    const input = el("input", { placeholder: "Search leads...", value: State.leadsSearch });
    input.addEventListener("input", (e) => { State.leadsSearch = e.target.value; renderTableBody(); });
    search.appendChild(input);
    toolbar.appendChild(search);
    view.appendChild(toolbar);

    const tableWrap = el("div", { id: "leadsTableWrap" });
    view.appendChild(tableWrap);

    function renderTableBody() {
      tableWrap.innerHTML = "";
      const activeView = SAVED_VIEWS.find((v) => v.id === State.leadsView) || SAVED_VIEWS[0];
      let rows = State.overview.filter(activeView.filter);
      if (State.leadsSearch.trim()) {
        const q = State.leadsSearch.trim().toLowerCase();
        rows = rows.filter((r) => r.name.toLowerCase().includes(q) || r.vertical.toLowerCase().includes(q));
      }
      rows = sortRows(rows, State.sortKey, State.sortDir);

      if (!rows.length) {
        tableWrap.appendChild(emptyState({ title: "No leads match this view.", sub: "Try a different saved view or clear your search." }));
        return;
      }

      const table = el("table", { class: "data-table" });
      const thead = el("thead", {}, el("tr", {}, [
        headerCell("Company", "name"), headerCell("Opportunity", null), headerCell("Confidence", "confidence"),
        headerCell("Evidence", "evidence_count"), headerCell("Decision", null), headerCell("Status", "status"),
        headerCell("Updated", "updated"),
      ]));
      table.appendChild(thead);
      const tbody = el("tbody");
      rows.forEach((r) => {
        const tr = el("tr", { "data-lead-id": r.id, tabindex: "0" }, [
          el("td", {}, [el("div", { class: "cell-name" }, r.name), el("div", { class: "cell-sub" }, r.vertical)]),
          el("td", {}, r.recommended_action ? opportunityLabel(r) : el("span", { class: "cell-sub" }, "—")),
          el("td", {}, confidenceMeter(r.confidence)),
          el("td", {}, `${r.evidence_count} sources`),
          el("td", {}, decisionPill(r.recommended_action)),
          el("td", {}, statusPill(r.status)),
          el("td", { class: "cell-sub" }, r.last_event ? fmtAgo(r.last_event.ts) : "—"),
        ]);
        tr.addEventListener("click", () => openRecordPanel(r.id));
        tr.addEventListener("keydown", (e) => {
          const focusable = $$("tbody tr", table);
          const idx = focusable.indexOf(document.activeElement);
          if (e.key === "Enter") { e.preventDefault(); openRecordPanel(r.id); }
          else if (e.key === "ArrowDown") { e.preventDefault(); focusable[idx + 1]?.focus(); }
          else if (e.key === "ArrowUp") { e.preventDefault(); focusable[idx - 1]?.focus(); }
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      tableWrap.appendChild(table);
    }

    function headerCell(label, key) {
      const th = el("th", {}, label + (State.sortKey === key && key ? (State.sortDir === "asc" ? " ↑" : " ↓") : ""));
      if (key) th.addEventListener("click", () => {
        if (State.sortKey === key) State.sortDir = State.sortDir === "asc" ? "desc" : "asc";
        else { State.sortKey = key; State.sortDir = "desc"; }
        renderTableBody();
      });
      return th;
    }

    renderTableBody();
  }

  function sortRows(rows, key, dir) {
    const mul = dir === "asc" ? 1 : -1;
    const copy = [...rows];
    copy.sort((a, b) => {
      let av, bv;
      if (key === "updated") { av = a.last_event?.ts || 0; bv = b.last_event?.ts || 0; }
      else { av = a[key]; bv = b[key]; }
      if (av === null || av === undefined) av = -Infinity;
      if (bv === null || bv === undefined) bv = -Infinity;
      if (typeof av === "string") return av.localeCompare(bv) * mul;
      return (av - bv) * mul;
    });
    return copy;
  }

  function opportunityLabel(r) {
    const map = { ACT: "Verified defect", HUMAN_REVIEW: "Needs review", CLOSE: "No opportunity" };
    return map[r.recommended_action] || r.recommended_action;
  }

  function decisionPill(action) {
    if (!action) return el("span", { class: "pill pill-neutral" }, "—");
    const map = { ACT: "pill-green", HUMAN_REVIEW: "pill-amber", CLOSE: "pill-neutral" };
    return el("span", { class: `pill ${map[action] || "pill-neutral"}` }, action.replace(/_/g, " "));
  }

  function svgSmall(name) {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "13"); s.setAttribute("height", "13");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
    s.innerHTML = name === "search" ? '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>' : "";
    return s;
  }

  // ---------------------------------------------------------------
  // INBOX (honest placeholder — no backend endpoint exists yet)
  // ---------------------------------------------------------------
  function renderInbox(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Inbox"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Replies and notifications from outreach."));
    view.appendChild(emptyState({
      title: "Inbox isn't wired up yet.",
      sub: "ProofFirst doesn't currently have a reply-tracking backend — outreach messages are generated but replies aren't ingested. This is an honest placeholder, not a mock.",
    }));
  }

  // ---------------------------------------------------------------
  // RECORD SIDE PANEL
  // ---------------------------------------------------------------
  async function openRecordPanel(leadId, liveEntries) {
    State.selectedLeadId = leadId;
    const basic = State.overview.find((r) => r.id === leadId) || {};
    openPanel({ eyebrow: "Company", title: basic.name || leadId, wide: false });

    const body = $("#panelBody");
    body.innerHTML = "";
    const actionsBar = $("#panelActions");
    actionsBar.classList.remove("hidden");
    actionsBar.innerHTML = "";

    const runBtn = el("button", { class: "btn btn-primary btn-sm" }, basic.status === "not_started" ? "Research" : "Re-run research");
    runBtn.addEventListener("click", () => runResearch(leadId, runBtn));
    actionsBar.appendChild(runBtn);
    const evBtn = el("button", { class: "btn btn-sm" }, "Review evidence");
    evBtn.addEventListener("click", () => { closePanel(); navigate("evidence", { evidenceLeadId: leadId }); });
    actionsBar.appendChild(evBtn);
    const graphBtn = el("button", { class: "btn btn-sm" }, "Proof graph");
    graphBtn.addEventListener("click", () => openProofGraphPanel(leadId));
    actionsBar.appendChild(graphBtn);
    if (basic.action_status === "PENDING_APPROVAL") {
      const apBtn = el("button", { class: "btn btn-sm" }, "Review action");
      apBtn.addEventListener("click", () => openApprovalPanel(leadId, basic.action_id));
      actionsBar.appendChild(apBtn);
    }

    body.appendChild(el("div", { class: "field-grid" }, [
      el("div", { class: "field-label" }, "Vertical"), el("div", { class: "field-value" }, basic.vertical || "—"),
      el("div", { class: "field-label" }, "Confidence"), el("div", { class: "field-value" }, confidenceMeter(basic.confidence)),
      el("div", { class: "field-label" }, "Decision"), el("div", { class: "field-value" }, decisionPill(basic.recommended_action)),
      el("div", { class: "field-label" }, "Status"), el("div", { class: "field-value" }, statusPill(basic.status)),
    ]));

    body.appendChild(el("div", { class: "section-label" }, "Activity"));
    const tlWrap = el("div", { class: "timeline" });
    body.appendChild(tlWrap);
    tlWrap.appendChild(el("div", { class: "skeleton skeleton-row" }));

    try {
      const { activity } = await api(`/api/leads/${leadId}/activity`);
      tlWrap.innerHTML = "";
      if (!activity.length) {
        tlWrap.replaceWith(emptyState({ title: "No evidence collected yet.", sub: "Run research to begin." }));
        return;
      }
      const ordered = activity.slice().reverse();
      ordered.forEach((ev, i) => {
        // "Live" = this event happened during the run that just finished
        // (liveEntries), so it staggers in; older history renders instantly.
        const isLive = liveEntries && liveEntries.has(ev.ts);
        tlWrap.appendChild(timelineItem(ev, isLive, i));
      });
    } catch (e) {
      tlWrap.innerHTML = "";
      tlWrap.appendChild(errorBanner("Couldn't load activity", e.message));
    }

    // "Live call updates" section — same data source the widget polls
    // (single source of truth), fetched once on open; the widget's own
    // poll loop keeps it fresh afterwards while this panel stays open.
    try {
      const { live_requests } = await api(`/api/leads/${leadId}/live-requests`);
      renderLiveCallPanelSection(leadId, live_requests);
    } catch (_) { /* no live-call activity for this lead yet — fine, section just doesn't render */ }
  }

  // `live` + `staggerIndex`: only set when entries are being appended after
  // a real run just completed (see runResearch below) — NOT on initial load,
  // so the fade+stagger reads as "watching the pipeline think just now",
  // not as decoration on every page visit.
  function timelineItem(ev, live, staggerIndex) {
    const tone = ev.message.toLowerCase().includes("block") || ev.message.toLowerCase().includes("contradict") ? "t-red"
      : ev.message.toLowerCase().includes("approv") || ev.message.toLowerCase().includes("success") || ev.message.toLowerCase().includes("verified") ? "t-green"
      : ev.message.toLowerCase().includes("review") ? "t-amber" : "t-blue";
    const meta = AGENT_META[ev.agent] || { icon: "search" };
    const item = el("div", { class: `timeline-item ${tone}${live ? " live-enter" : ""}` }, [
      el("div", { class: "timeline-dot" }, svgSmall(meta.icon)),
      el("div", { class: "timeline-time" }, new Date(ev.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })),
      el("div", { class: "timeline-agent" }, [agentDot(ev.agent), ev.agent]),
      el("div", { class: "timeline-msg" }, ev.message),
    ]);
    if (live) item.style.setProperty("--stagger", `${staggerIndex * 60}ms`);
    return item;
  }

  function errorBanner(title, body) {
    return el("div", { class: "error-banner" }, [
      svgIcon("search"),
      el("div", {}, [el("div", { class: "title" }, title), el("div", { class: "body" }, body)]),
    ]);
  }

  async function runResearch(leadId, btn) {
    // Structural double-fire guard: disabling the button covers mouse clicks,
    // but a fast double Enter/Space via keyboard or a screen reader can still
    // race the disabled-state paint. State.inFlightRuns is the actual guard.
    if (State.inFlightRuns.has(leadId)) return;
    State.inFlightRuns.add(leadId);

    const original = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = "";
    btn.appendChild(el("span", { class: "spinner" }));
    btn.append(" Running…");
    let beforeTs = new Set();
    try {
      const before = await api(`/api/leads/${leadId}/activity`).catch(() => ({ activity: [] }));
      beforeTs = new Set(before.activity.map((e) => e.ts));
    } catch (_) {}
    try {
      await api(`/api/leads/${leadId}/run`, { method: "POST" });
      toast(`Research pipeline finished for ${State.overview.find((r) => r.id === leadId)?.name || leadId}`);
      await loadOverview({ silent: true });
      // Always go through the router's render() so the view container is
      // cleared before redraw — calling a view function directly (e.g.
      // renderLeads($("#view"))) appends on top of what's already there
      // instead of replacing it, and stacks duplicate tables.
      refreshCurrentView();
      flashRow(leadId);
      if ($("#sidePanel").classList.contains("open") && State.selectedLeadId === leadId) {
        const after = await api(`/api/leads/${leadId}/activity`).catch(() => ({ activity: [] }));
        const newTs = new Set(after.activity.filter((e) => !beforeTs.has(e.ts)).map((e) => e.ts));
        openRecordPanel(leadId, newTs);
      }
    } catch (e) {
      toast(`Research failed: ${e.message}`);
    } finally {
      State.inFlightRuns.delete(leadId);
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  // Single place every in-place mutation (run / approve / reject) goes
  // through to get back on screen — always the dispatcher, never a bare
  // view function, so the container is guaranteed to be cleared first.
  function refreshCurrentView() {
    render();
  }

  // ---------------------------------------------------------------
  // EVIDENCE HUB
  // ---------------------------------------------------------------
  async function renderEvidenceHub(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Evidence"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Claims ProofFirst has investigated, cross-checked against an independent second source."));

    const picker = el("div", { class: "search-box", style: "margin-bottom:16px; max-width:340px;" });
    const select = el("select", { style: "border:none; outline:none; background:transparent; font-size:12.5px; width:100%; color:var(--text);" });
    select.appendChild(el("option", { value: "" }, "Select a lead…"));
    State.overview.filter((r) => r.claim_count > 0).forEach((r) => {
      const opt = el("option", { value: r.id }, `${r.name} (${r.claim_count} claims)`);
      select.appendChild(opt);
    });
    if (State.evidenceLeadId) select.value = State.evidenceLeadId;
    picker.appendChild(select);
    view.appendChild(picker);

    const shellWrap = el("div", { id: "evidenceShellWrap" });
    view.appendChild(shellWrap);

    select.addEventListener("change", () => {
      State.evidenceLeadId = select.value || null;
      State.evidenceClaimId = null;
      loadEvidenceShell(shellWrap);
    });

    if (!State.evidenceLeadId && State.overview.some((r) => r.claim_count > 0)) {
      State.evidenceLeadId = State.overview.find((r) => r.claim_count > 0).id;
      select.value = State.evidenceLeadId;
    }

    if (!State.evidenceLeadId) {
      shellWrap.appendChild(emptyState({ title: "No evidence collected yet.", sub: "Run research on a lead to begin building an evidence dossier.", cta: "Go to Leads", onCta: () => navigate("leads") }));
      return;
    }
    loadEvidenceShell(shellWrap);
  }

  async function loadEvidenceShell(wrap) {
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "skeleton skeleton-row", style: "height:400px;" }));
    let dossier;
    try {
      const res = await api(`/api/leads/${State.evidenceLeadId}/run`, { method: "GET" }).catch(() => null);
      // /run is POST-only; fall back to activity-derived dossier via overview + direct fetch is not exposed as GET.
    } catch (_) {}
    // There's no GET dossier endpoint exposed publicly besides via /run (POST, re-runs pipeline) —
    // so we read the already-persisted dossier the honest way: re-fetch overview claim counts and
    // fall back to activity if a dedicated fetch isn't available.
    try {
      dossier = await fetchDossierHonestly(State.evidenceLeadId);
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(errorBanner("Couldn't load evidence", e.message));
      return;
    }
    wrap.innerHTML = "";
    if (!dossier || !dossier.claims || !dossier.claims.length) {
      wrap.appendChild(emptyState({ title: "No evidence collected yet.", sub: "Run research to begin." }));
      return;
    }
    renderEvidenceShell(wrap, dossier);
  }

  // The server only exposes the dossier via POST /run (which re-executes the pipeline) — there is
  // no idempotent GET. Calling /run again is safe (orchestrator + storage dedupe actions), so we
  // use it to honestly fetch current dossier state without inventing a fake endpoint.
  async function fetchDossierHonestly(leadId) {
    const res = await api(`/api/leads/${leadId}/run`, { method: "POST" });
    return res.dossier;
  }

  function renderEvidenceShell(wrap, dossier) {
    const shell = el("div", { class: "evidence-shell fade-in" });
    const claimsCol = el("div", { class: "claims-col" });
    const detailCol = el("div", { class: "claim-detail" });
    shell.appendChild(claimsCol);
    shell.appendChild(detailCol);
    wrap.appendChild(shell);

    if (!State.evidenceClaimId || !dossier.claims.find((c) => c.id === State.evidenceClaimId)) {
      State.evidenceClaimId = dossier.claims[0].id;
    }

    dossier.claims.forEach((c) => {
      const icon = c.verdict === "CORROBORATED" ? "✓" : c.verdict === "CONTRADICTED" ? "✕" : "?";
      const color = c.verdict === "CORROBORATED" ? "var(--green)" : c.verdict === "CONTRADICTED" ? "var(--red)" : "var(--amber)";
      const row = el("div", { class: `claim-row ${c.id === State.evidenceClaimId ? "selected" : ""}` }, [
        el("span", { class: "claim-icon", style: `color:${color}; font-weight:700;` }, icon),
        el("div", {}, [
          el("div", { class: "claim-text" }, c.text),
          el("div", { class: "claim-sub" }, `${c.origin_agent} · ${fmtPct(c.confidence)} confidence`),
        ]),
      ]);
      row.addEventListener("click", () => {
        State.evidenceClaimId = c.id;
        $$(".claim-row", claimsCol).forEach((r) => r.classList.remove("selected"));
        row.classList.add("selected");
        renderClaimDetail(detailCol, dossier.claims.find((cl) => cl.id === c.id));
      });
      claimsCol.appendChild(row);
    });

    renderClaimDetail(detailCol, dossier.claims.find((c) => c.id === State.evidenceClaimId));
  }

  function renderClaimDetail(col, claim) {
    col.innerHTML = "";
    col.appendChild(el("div", { class: "claim-detail-title fade-in" }, `"${claim.text}"`));

    // Trace: Research -> Skeptic -> Verdict
    const trace = el("div", {});
    trace.appendChild(traceStep("ResearchAgent", "Single-pass search.", claim.origin_agent === "ResearchAgent"));
    trace.appendChild(traceStep("SkepticAgent", claim.contradicting_evidence.length ? "Searched independent sources — found contradictory evidence." : "Searched independent sources — no contradictions found.", true, claim.supporting_evidence.concat(claim.contradicting_evidence)));
    col.appendChild(trace);

    const hasBoth = claim.supporting_evidence.length && claim.contradicting_evidence.length;
    if (hasBoth) {
      // Independent sources disagreed on this claim — this is the case
      // "evidence-first" as a product claim rests on, so give it its own
      // side-by-side layout instead of the generic stacked list below.
      col.appendChild(el("div", { class: "section-label", style: "margin-top:6px;" }, "Sources disagreed"));
      col.appendChild(el("div", { class: "disagreement-banner" }, [
        svgSmall("search"),
        `Independent sources gave conflicting evidence — ${claim.supporting_evidence.length} supporting vs. ${claim.contradicting_evidence.length} contradicting.`,
      ]));
      const twoSided = el("div", { class: "two-sided" });
      const supCol = el("div", {}, [el("div", { class: "side-label supports" }, "Supports")]);
      const conCol = el("div", {}, [el("div", { class: "side-label contradicts" }, "Contradicts")]);
      claim.supporting_evidence.forEach((ev) => supCol.appendChild(evidenceExcerpt(ev, true)));
      claim.contradicting_evidence.forEach((ev) => conCol.appendChild(evidenceExcerpt(ev, false)));
      twoSided.appendChild(supCol);
      twoSided.appendChild(conCol);
      col.appendChild(twoSided);
    } else if (claim.supporting_evidence.length) {
      col.appendChild(el("div", { class: "section-label", style: "margin-top:6px;" }, "Supporting evidence"));
      claim.supporting_evidence.forEach((ev) => col.appendChild(evidenceExcerpt(ev, true)));
    } else if (claim.contradicting_evidence.length) {
      col.appendChild(el("div", { class: "section-label" }, "Contradicting evidence"));
      claim.contradicting_evidence.forEach((ev) => col.appendChild(evidenceExcerpt(ev, false)));
    }

    const verdictStyle = { CORROBORATED: ["var(--green)", "var(--green-bg)", "var(--green-border)"], CONTRADICTED: ["var(--red)", "var(--red-bg)", "var(--red-border)"], UNVERIFIABLE: ["var(--amber)", "var(--amber-bg)", "var(--amber-border)"] };
    const [color, bg, border] = verdictStyle[claim.verdict] || verdictStyle.UNVERIFIABLE;
    const banner = el("div", { class: "verdict-banner", style: `color:${color}; background:${bg}; border-color:${border};` }, [
      el("div", {}, [el("div", { class: "vb-label" }, "Verdict"), el("div", { class: "vb-verdict" }, claim.verdict || "PENDING")]),
      el("div", {}, [el("div", { class: "vb-label" }, "Confidence"), el("div", { class: "vb-verdict" }, fmtPct(claim.confidence))]),
    ]);
    col.appendChild(banner);
    if (claim.reasoning) col.appendChild(el("div", { class: "excerpt-card", style: "font-style:normal; margin-top:10px;" }, claim.reasoning));
  }

  function traceStep(agent, body, done, sources) {
    const wrap = el("div", { class: "trace-step" });
    wrap.appendChild(el("div", { class: "trace-rail" }, [
      el("div", { class: "trace-dot" }, done ? svgSmall("search") : el("span", { class: "spinner" })),
      el("div", { class: "trace-line" }),
    ]));
    const content = el("div", { class: "trace-content" }, [
      el("div", { class: "trace-agent" }, agent),
      el("div", { class: "trace-body" }, body),
    ]);
    if (sources && sources.length) {
      const chips = el("div", {});
      const seen = new Set();
      sources.forEach((s) => { if (!seen.has(s.source)) { seen.add(s.source); chips.appendChild(el("span", { class: "source-chip" }, s.source.replace(/_/g, " "))); } });
      content.appendChild(chips);
    }
    wrap.appendChild(content);
    return wrap;
  }

  function evidenceExcerpt(ev, supports) {
    const card = el("div", { class: "excerpt-card" }, [
      `"${ev.text}"`,
      el("div", { class: "excerpt-source" }, `${ev.source.replace(/_/g, " ")} · ${supports ? "supports" : "contradicts"} · ${new Date(ev.retrieved_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`),
    ]);
    return card;
  }

  // ---------------------------------------------------------------
  // PROOF GRAPH — Task → Claim → Evidence → Decision → Action →
  // Verification → Outcome, so a click-through from the final outcome
  // back to the specific evidence that caused it is always available.
  // Purely additive read from /api/leads/{id}/proof-graph; renders as a
  // native collapsible <details> tree (no graphics library, per spec).
  // ---------------------------------------------------------------
  async function openProofGraphPanel(leadId) {
    const basic = State.overview.find((r) => r.id === leadId) || {};
    openPanel({ eyebrow: "Proof graph", title: basic.name || leadId, wide: true });

    const body = $("#panelBody");
    body.innerHTML = "";
    $("#panelActions").classList.add("hidden");

    body.appendChild(el("p", { class: "page-subtitle", style: "margin-top:0;" },
      "Task → Claim → Evidence → Decision → Action → Verification → Outcome. Click any row to expand or collapse it."));

    const wrap = el("div", { class: "proof-tree" });
    body.appendChild(wrap);
    wrap.appendChild(el("div", { class: "skeleton skeleton-row" }));

    try {
      const graph = await api(`/api/leads/${leadId}/proof-graph`);
      wrap.innerHTML = "";
      wrap.appendChild(renderProofNode(graph.root));
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(errorBanner("Couldn't load proof graph", e.message));
    }
  }

  function proofNodeBadge(node) {
    let color = "var(--text-tertiary)", bg = "var(--surface-raised)", border = "var(--border)";
    const status = node.status || "";
    if (node.node_type === "Evidence") {
      [color, bg, border] = status === "SUPPORTS"
        ? ["var(--green)", "var(--green-bg)", "var(--green-border)"]
        : ["var(--red)", "var(--red-bg)", "var(--red-border)"];
    } else if (node.node_type === "Claim") {
      if (status === "CORROBORATED") [color, bg, border] = ["var(--green)", "var(--green-bg)", "var(--green-border)"];
      else if (status === "CONTRADICTED") [color, bg, border] = ["var(--red)", "var(--red-bg)", "var(--red-border)"];
      else if (status === "UNVERIFIABLE") [color, bg, border] = ["var(--amber)", "var(--amber-bg)", "var(--amber-border)"];
    } else if (node.node_type === "Task" || node.node_type === "Decision") {
      [color, bg, border] = ["var(--blue)", "var(--blue-bg)", "var(--blue-border)"];
    } else if (["Action", "Verification", "Outcome"].includes(node.node_type)) {
      if (status === "SUCCESS") [color, bg, border] = ["var(--green)", "var(--green-bg)", "var(--green-border)"];
      else if (["FAILED", "BLOCKED", "REJECTED", "ROLLED_BACK"].includes(status)) [color, bg, border] = ["var(--red)", "var(--red-bg)", "var(--red-border)"];
      else if (["PENDING_APPROVAL", "APPROVED", "EXECUTED"].includes(status)) [color, bg, border] = ["var(--amber)", "var(--amber-bg)", "var(--amber-border)"];
      else [color, bg, border] = ["var(--blue)", "var(--blue-bg)", "var(--blue-border)"];
    }
    return el("span", { class: "proof-node-type", style: `color:${color}; background:${bg}; border-color:${border};` }, node.node_type);
  }

  function renderProofNode(node) {
    const summary = el("summary", { class: "proof-node-summary" }, [
      proofNodeBadge(node),
      el("span", { class: "proof-node-title" }, node.title || "(untitled)"),
    ]);
    const details = el("details", { class: "proof-node", open: "" }, [summary]);
    if (node.detail) details.appendChild(el("div", { class: "proof-node-detail" }, node.detail));
    if (node.children && node.children.length) {
      const childWrap = el("div", { class: "proof-node-children" });
      node.children.forEach((c) => childWrap.appendChild(renderProofNode(c)));
      details.appendChild(childWrap);
    }
    return details;
  }

  // ---------------------------------------------------------------
  // ACTIONS
  // ---------------------------------------------------------------
  function renderActions(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Actions"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Everything ProofFirst wants to do in the real world — nothing executes without approval."));

    const groups = { PENDING_APPROVAL: [], BLOCKED: [], APPROVED: [], EXECUTED: [], SUCCESS: [], FAILED: [], REJECTED: [], ROLLED_BACK: [] };
    State.overview.forEach((r) => { if (r.action_status && groups[r.action_status]) groups[r.action_status].push(r); });

    const sections = [
      ["Pending approval", groups.PENDING_APPROVAL.concat(groups.APPROVED)],
      ["Blocked", groups.BLOCKED],
      ["Completed", groups.SUCCESS.concat(groups.EXECUTED)],
      ["Failed / rolled back", groups.FAILED.concat(groups.ROLLED_BACK, groups.REJECTED)],
    ];

    let any = false;
    sections.forEach(([label, rows]) => {
      if (!rows.length) return;
      any = true;
      view.appendChild(el("div", { class: "section-label" }, label));
      const list = el("div", { class: "work-list" });
      rows.forEach((r) => {
        const risk = r.status === "blocked" ? "Blocked" : "Low";
        const row = el("div", { class: "work-row", style: "grid-template-columns: 1.7fr 1fr 0.8fr 0.9fr 0.7fr;", "data-lead-id": r.id }, [
          el("div", {}, [el("div", { class: "work-lead-name" }, r.name), el("div", { class: "work-lead-sub" }, r.vertical)]),
          el("div", { class: "work-meta" }, r.recommended_action ? opportunityLabel(r) : "—"),
          confidenceMeter(r.confidence),
          el("div", {}, actionStatusPill(r.action_status)),
          el("div", { class: "work-time" }, r.last_event ? fmtAgo(r.last_event.ts) : "—"),
        ]);
        row.addEventListener("click", () => openApprovalPanel(r.id, r.action_id));
        list.appendChild(row);
      });
      view.appendChild(list);
    });

    if (!any) {
      view.appendChild(emptyState({ title: "You're clear.", sub: "No verified actions are waiting for approval." }));
    }
  }

  function actionStatusPill(status) {
    const map = {
      PENDING_APPROVAL: ["pill-amber", "Awaiting approval"], BLOCKED: ["pill-red", "Blocked"],
      APPROVED: ["pill-blue", "Approved"], EXECUTED: ["pill-blue", "Executing"], SUCCESS: ["pill-green", "Success"],
      FAILED: ["pill-red", "Failed"], REJECTED: ["pill-neutral", "Rejected"], ROLLED_BACK: ["pill-amber", "Rolled back"],
    };
    const [cls, label] = map[status] || ["pill-neutral", status || "—"];
    return el("span", { class: `pill ${cls}` }, [el("span", { class: "pill-dot" }), label]);
  }

  async function openApprovalPanel(leadId, actionId) {
    const basic = State.overview.find((r) => r.id === leadId) || {};
    openPanel({ eyebrow: "Proposed action", title: basic.name || leadId, wide: true });
    const body = $("#panelBody");
    body.innerHTML = "";
    $("#panelActions").classList.add("hidden");

    if (!actionId) {
      body.appendChild(emptyState({ title: "No proposed action yet.", sub: "Run research on this lead — approval requests only appear once ProofFirst has verified a real defect." }));
      return;
    }
    // Loading skeleton that actually looks like the layout about to appear,
    // not just a single bar — so the panel doesn't read as frozen/broken
    // during the fetch.
    body.appendChild(panelLoadingSkeleton());

    let dossier, action;
    try {
      // Read-only: GET /action never re-runs the pipeline or inserts a new
      // action row. Only the explicit "Research" / "Re-run research" button
      // (see runResearch, bound to POST /run) is allowed to trigger a real
      // pipeline execution — just opening this panel to look must not.
      const res = await api(`/api/leads/${leadId}/action`);
      dossier = res.dossier;
      action = res.action;
    } catch (e) {
      body.innerHTML = "";
      body.appendChild(errorBanner("Couldn't load action", e.message));
      return;
    }
    body.innerHTML = "";

    if (!action) {
      body.appendChild(emptyState({ title: "No action is currently proposed.", sub: `Decision: ${dossier.recommended_action || "n/a"}. ProofFirst only proposes an action when the evidence clears the bar for outreach.` }));
      return;
    }

    const claim = dossier.claims.find((c) => c.id === action.defect_claim_id);

    body.appendChild(el("div", { class: "approval-section" }, [
      el("div", { class: "al" }, "Why this action?"),
      el("p", {}, claim ? `The evidence dossier verified: "${claim.text}" (${fmtPct(claim.confidence)} confidence, ${claim.verdict}).` : action.description),
    ]));
    body.appendChild(el("div", { class: "approval-section" }, [
      el("div", { class: "al" }, "What will happen"),
      el("p", {}, action.description),
    ]));

    const risk = el("div", { class: "risk-grid" }, [
      el("div", { class: "risk-card" }, [el("div", { class: "rk" }, "Risk"), el("div", { class: "rv" }, action.risk)]),
      el("div", { class: "risk-card" }, [el("div", { class: "rk" }, "Reversible"), el("div", { class: "rv" }, action.reversible ? "Yes" : "No")]),
    ]);
    body.appendChild(risk);

    if (action.block_reason) {
      body.appendChild(errorBanner("Blocked by preflight guardrails", action.block_reason));
    }

    if (claim) {
      body.appendChild(el("div", { class: "section-label" }, "Sources"));
      claim.supporting_evidence.concat(claim.contradicting_evidence).forEach((ev) => body.appendChild(evidenceExcerpt(ev, ev.supports)));
    }

    if (action.status === "PENDING_APPROVAL") {
      const cta = el("div", { class: "approval-cta" });
      const approve = el("button", { class: "btn btn-primary" }, "Approve");
      const reject = el("button", { class: "btn btn-danger" }, "Reject");
      approve.addEventListener("click", () => decide(true));
      reject.addEventListener("click", () => decide(false));
      cta.appendChild(approve); cta.appendChild(reject);
      body.appendChild(cta);
    } else {
      body.appendChild(el("div", { class: "section-label" }, "Status"));
      body.appendChild(actionStatusPill(action.status));
    }

    async function decide(approved) {
      try {
        const res = await api(`/api/leads/${leadId}/approve`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action_id: action.id, approved }),
        });
        toast(approved ? "Action approved." : "Action rejected.");
        if (res.outreach_message) toast("Outreach message generated.");
        await loadOverview({ silent: true });
        openApprovalPanel(leadId, res.action.id);
        refreshCurrentView(); // dispatcher, not a bare view function — see runResearch
        flashRow(leadId);
      } catch (e) {
        toast(`Couldn't record decision: ${e.message}`);
      }
    }
  }

  function panelLoadingSkeleton() {
    const wrap = el("div", {});
    wrap.appendChild(el("div", { class: "skeleton", style: "height:14px; width:70%; margin-bottom:14px;" }));
    wrap.appendChild(el("div", { class: "skeleton", style: "height:60px; margin-bottom:16px;" }));
    wrap.appendChild(el("div", { class: "skeleton", style: "height:14px; width:40%; margin-bottom:8px;" }));
    wrap.appendChild(el("div", { class: "skeleton", style: "height:50px; margin-bottom:16px;" }));
    wrap.appendChild(el("div", { class: "skeleton", style: "height:80px;" }));
    return wrap;
  }

  // ---------------------------------------------------------------
  // AGENTS (derived from activity — no fake metrics)
  // ---------------------------------------------------------------
  const AGENT_PIPELINE = ["ResearchAgent", "SkepticAgent", "EvidenceSynthesisAgent", "DecisionAgent", "PreflightVerifierAgent", "ActionExecutor", "PostActionVerificationAgent", "OutreachAgent"];

  async function renderAgents(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Agents"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Every agent in the pipeline, with real run counts pulled from the activity log."));
    const grid = el("div", { class: "agent-grid" });
    view.appendChild(grid);
    AGENT_PIPELINE.forEach((name) => grid.appendChild(agentCardSkeleton(name)));

    try {
      const results = await Promise.all(State.overview.filter((r) => r.claim_count > 0 || r.action_status).map((r) => api(`/api/leads/${r.id}/activity`).then((res) => res.activity).catch(() => [])));
      const allActivity = results.flat();
      grid.innerHTML = "";
      AGENT_PIPELINE.forEach((name) => {
        const events = allActivity.filter((e) => e.agent === name);
        grid.appendChild(agentCard(name, events));
      });
    } catch (e) {
      grid.innerHTML = "";
      grid.appendChild(errorBanner("Couldn't load agent activity", e.message));
    }
  }

  function agentCardSkeleton(name) {
    return el("div", { class: "agent-card" }, [el("div", { class: "agent-name" }, name), el("div", { class: "skeleton", style: "height:14px; margin-top:8px;" })]);
  }

  function agentCard(name, events) {
    const last = events.sort((a, b) => b.ts - a.ts)[0];
    return el("div", { class: "agent-card fade-in" }, [
      el("div", { class: "agent-card-head" }, [
        el("div", { class: "agent-name" }, [agentDot(name), name]),
        el("span", { class: `pill ${events.length ? "pill-blue" : "pill-neutral"}` }, [el("span", { class: "pill-dot" }), events.length ? "Active" : "Idle"]),
      ]),
      el("div", { class: "agent-stat-row" }, [el("span", {}, "Runs logged"), el("b", {}, String(events.length))]),
      el("div", { class: "agent-stat-row" }, [el("span", {}, "Last run"), el("b", {}, last ? fmtAgo(last.ts) : "—")]),
      el("div", { class: "agent-stat-row" }, [el("span", {}, "Last result"), el("b", { style: "font-weight:550; text-align:right;" }, last ? last.message : "No runs yet")]),
    ]);
  }

  // ---------------------------------------------------------------
  // ACTIVITY (global)
  // ---------------------------------------------------------------
  async function renderActivity(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Activity"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Every agent event across every lead, most recent first."));
    const tl = el("div", { class: "timeline" });
    view.appendChild(tl);
    tl.appendChild(el("div", { class: "skeleton skeleton-row" }));

    try {
      const results = await Promise.all(State.overview.map((r) => api(`/api/leads/${r.id}/activity`).then((res) => res.activity.map((e) => ({ ...e, leadName: r.name }))).catch(() => [])));
      const all = results.flat().sort((a, b) => b.ts - a.ts);
      tl.innerHTML = "";
      if (!all.length) {
        tl.replaceWith(emptyState({ title: "Nothing challenged yet.", sub: "No agent activity has been logged. Run research on a lead to generate a trace." }));
        return;
      }
      all.slice(0, 200).forEach((ev) => {
        const item = timelineItem(ev);
        item.querySelector(".timeline-agent").textContent = `${ev.agent} · ${ev.leadName}`;
        item.style.cursor = "pointer";
        item.addEventListener("click", () => openRecordPanel(ev.lead_id));
        tl.appendChild(item);
      });
    } catch (e) {
      tl.innerHTML = "";
      tl.appendChild(errorBanner("Couldn't load activity", e.message));
    }
  }

  // ---------------------------------------------------------------
  // REPORTS (lightweight, derived — not a fake KPI dashboard)
  // ---------------------------------------------------------------
  function renderReports(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Reports"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Pipeline outcomes, computed live from stored dossiers and actions."));

    const rows = State.overview;
    const started = rows.filter((r) => r.status !== "not_started").length;
    const verified = rows.filter((r) => r.status === "verified").length;
    const blocked = rows.filter((r) => r.status === "blocked").length;
    const pending = rows.filter((r) => r.action_status === "PENDING_APPROVAL").length;

    const stats = [
      ["Leads researched", `${started} / ${rows.length}`],
      ["Verified actions", verified],
      ["Blocked / contradicted", blocked],
      ["Awaiting approval", pending],
    ];
    const grid = el("div", { class: "agent-grid" });
    stats.forEach(([label, value]) => grid.appendChild(el("div", { class: "agent-card" }, [
      el("div", { class: "agent-name", style: "color:var(--text-secondary); font-weight:550;" }, label),
      el("div", { style: "font-size:24px; font-weight:700; margin-top:6px; letter-spacing:-0.02em;" }, String(value)),
    ])));
    view.appendChild(grid);

    view.appendChild(el("div", { class: "section-label" }, "By status"));
    const statusCounts = {};
    rows.forEach((r) => { statusCounts[r.status] = (statusCounts[r.status] || 0) + 1; });
    const list = el("div", { class: "work-list" });
    Object.entries(statusCounts).forEach(([status, count]) => {
      list.appendChild(el("div", { class: "work-row", style: "grid-template-columns: 1fr auto;" }, [statusPill(status), el("div", { class: "work-meta" }, `${count} lead${count === 1 ? "" : "s"}`)]));
    });
    view.appendChild(list);
  }

  // ---------------------------------------------------------------
  // EVALUATION (hits existing /api/evaluation)
  // ---------------------------------------------------------------
  async function renderEvaluation(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Evaluation"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Automated evaluation of the decision pipeline against labeled fixtures."));
    const out = el("div", {});
    view.appendChild(out);
    out.appendChild(el("div", { class: "skeleton skeleton-row", style: "height:180px;" }));
    try {
      const res = await api("/api/evaluation");
      out.innerHTML = "";
      const pre = el("pre", { style: "background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:16px; font-size:11.5px; font-family:var(--font-mono); overflow-x:auto; white-space:pre-wrap;" }, JSON.stringify(res, null, 2));
      out.appendChild(pre);
    } catch (e) {
      out.innerHTML = "";
      out.appendChild(errorBanner("Evaluation failed", e.message));
    }
  }

  // ---------------------------------------------------------------
  // SETTINGS (honest placeholder)
  // ---------------------------------------------------------------
  function renderSettings(view) {
    view.appendChild(el("h1", { class: "page-title" }, "Settings"));
    view.appendChild(el("p", { class: "page-subtitle" }, "Environment and pipeline configuration."));
    view.appendChild(el("div", { class: "field-grid", style: "max-width:420px;" }, [
      el("div", { class: "field-label" }, "LLM mode"), el("div", { class: "field-value" }, State.mockMode ? "Mock (no live LLM key detected)" : "Live"),
      el("div", { class: "field-label" }, "Leads tracked"), el("div", { class: "field-value" }, String(State.leadsBasic.length)),
      el("div", { class: "field-label" }, "Action whitelist"), el("div", { class: "field-value mono" }, "propose_listing_correction, generate_booking_preview, generate_corrected_artifact"),
    ]));
  }

  // ---------------------------------------------------------------
  // SIDE PANEL primitives
  // ---------------------------------------------------------------
  function openPanel({ eyebrow, title, wide }) {
    $("#panelEyebrow").textContent = eyebrow || "";
    $("#panelTitle").textContent = title || "";
    $("#sidePanel").classList.toggle("wide", !!wide);
    $("#sidePanel").classList.add("open");
    $("#overlay").classList.add("open");
    // The record/approval panel is right-anchored, same corner as the
    // Live Call widget - without this, an open panel silently sits
    // UNDER the widget (widget has a higher z-index) and everything
    // appended to #panelBody past the fold, including the "Live call
    // updates" section, is unreachable. Slide the widget clear of the
    // panel's width instead of stacking them.
    const widget = $(".live-call-widget");
    if (widget) widget.classList.toggle("beside-wide-panel", !!wide), widget.classList.add("beside-panel");
  }
  function closePanel() {
    $("#sidePanel").classList.remove("open");
    $("#overlay").classList.remove("open");
    const widget = $(".live-call-widget");
    if (widget) widget.classList.remove("beside-panel", "beside-wide-panel");
  }
  $("#panelClose").addEventListener("click", closePanel);
  $("#overlay").addEventListener("click", closePanel);

  // ---------------------------------------------------------------
  // COMMAND PALETTE
  // ---------------------------------------------------------------
  let cmdkActiveIndex = 0;
  function openCmdk() {
    $("#cmdkOverlay").classList.add("open");
    $("#cmdkInput").value = "";
    $("#cmdkInput").focus();
    renderCmdkResults("");
  }
  function closeCmdk() { $("#cmdkOverlay").classList.remove("open"); }

  function renderCmdkResults(query) {
    const list = $("#cmdkList");
    list.innerHTML = "";
    cmdkActiveIndex = 0;
    const q = query.trim().toLowerCase();

    const staticActions = [
      { label: "Run research", sub: "Pick a lead", icon: "search", action: () => { closeCmdk(); navigate("leads"); } },
      { label: "Review pending actions", sub: `${State.overview.filter((r) => r.action_status === "PENDING_APPROVAL").length} waiting`, icon: "check", action: () => { closeCmdk(); navigate("actions"); } },
      { label: "View blocked leads", sub: `${State.overview.filter((r) => r.status === "blocked").length} blocked`, icon: "search", action: () => { closeCmdk(); navigate("leads", { leadsView: "contradicted" }); } },
      { label: "Run evaluation", sub: "Score the pipeline", icon: "check", action: () => { closeCmdk(); navigate("evaluation"); } },
      { label: "Go to Evidence", sub: "Browse claims", icon: "search", action: () => { closeCmdk(); navigate("evidence"); } },
    ];

    if (!q) {
      const recent = [...State.overview].filter((r) => r.last_event).sort((a, b) => b.last_event.ts - a.last_event.ts).slice(0, 4);
      if (recent.length) {
        list.appendChild(el("div", { class: "cmdk-group-label" }, "Recent"));
        recent.forEach((r) => list.appendChild(cmdkItem(r.name, r.vertical, () => { closeCmdk(); openRecordPanel(r.id); })));
      }
      list.appendChild(el("div", { class: "cmdk-group-label" }, "Actions"));
      staticActions.forEach((a) => list.appendChild(cmdkItem(a.label, a.sub, a.action)));
      refreshCmdkActive(list);
      return;
    }

    const leadMatches = State.overview.filter((r) => r.name.toLowerCase().includes(q) || r.vertical.toLowerCase().includes(q)).slice(0, 6);
    if (leadMatches.length) {
      list.appendChild(el("div", { class: "cmdk-group-label" }, "Leads"));
      leadMatches.forEach((r) => list.appendChild(cmdkItem(r.name, r.vertical, () => { closeCmdk(); openRecordPanel(r.id); })));
    }
    const actionMatches = staticActions.filter((a) => a.label.toLowerCase().includes(q));
    if (actionMatches.length) {
      list.appendChild(el("div", { class: "cmdk-group-label" }, "Actions"));
      actionMatches.forEach((a) => list.appendChild(cmdkItem(a.label, a.sub, a.action)));
    }
    if (!leadMatches.length && !actionMatches.length) {
      list.appendChild(el("div", { class: "cmdk-group-label" }, "No results"));
    }
    refreshCmdkActive(list);
  }

  function cmdkItem(label, sub, onClick, hint) {
    const item = el("div", { class: "cmdk-item" }, [
      svgIconSm(),
      el("span", {}, label),
      el("span", { class: "cmdk-sub" }, sub || ""),
      el("kbd", { class: "cmdk-hint" }, hint || "↵"),
    ]);
    item.addEventListener("click", onClick);
    item.addEventListener("mousemove", () => { $$(".cmdk-item").forEach((i) => i.classList.remove("active")); item.classList.add("active"); });
    return item;
  }
  function svgIconSm() {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.innerHTML = '<circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2"/><path d="M21 21l-4.3-4.3" fill="none" stroke="currentColor" stroke-width="2"/>';
    return s;
  }
  function refreshCmdkActive(list) {
    const items = $$(".cmdk-item", list);
    items.forEach((i, idx) => i.classList.toggle("active", idx === cmdkActiveIndex));
  }

  $("#cmdkInput").addEventListener("input", (e) => renderCmdkResults(e.target.value));
  $("#cmdkOverlay").addEventListener("click", (e) => { if (e.target.id === "cmdkOverlay") closeCmdk(); });
  $("#openCmdkFromSidebar").addEventListener("click", openCmdk);

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdk(); return; }
    if (e.key === "Escape") {
      if ($("#cmdkOverlay").classList.contains("open")) closeCmdk();
      else if ($("#sidePanel").classList.contains("open")) closePanel();
      return;
    }
    if ($("#cmdkOverlay").classList.contains("open")) {
      const items = $$(".cmdk-item");
      if (e.key === "ArrowDown") { e.preventDefault(); cmdkActiveIndex = Math.min(cmdkActiveIndex + 1, items.length - 1); refreshCmdkActive($("#cmdkList")); }
      if (e.key === "ArrowUp") { e.preventDefault(); cmdkActiveIndex = Math.max(cmdkActiveIndex - 1, 0); refreshCmdkActive($("#cmdkList")); }
      if (e.key === "Enter") { e.preventDefault(); items[cmdkActiveIndex]?.click(); }
      return;
    }
    // keyboard-first nav: G then letter
    if (e.key.toLowerCase() === "g" && !e.metaKey && !e.ctrlKey && document.activeElement.tagName !== "INPUT") {
      window.__gPending = true;
      setTimeout(() => { window.__gPending = false; }, 900);
      return;
    }
    if (window.__gPending && document.activeElement.tagName !== "INPUT") {
      const map = { l: "leads", e: "evidence", a: "actions", h: "home" };
      if (map[e.key.toLowerCase()]) { navigate(map[e.key.toLowerCase()]); }
      window.__gPending = false;
    }
  });

  // ---------------------------------------------------------------
  // ASK PROOFFIRST — answers derived from real overview/activity data,
  // not a scripted fake chatbot.
  // ---------------------------------------------------------------
  function openAsk() {
    openPanel({ eyebrow: "AI assistant", title: "Ask ProofFirst", wide: false });
    window.__askPanelOpen = true;
    const body = $("#panelBody");
    $("#panelActions").classList.add("hidden");
    body.innerHTML = "";
    body.appendChild(el("p", {}, "Ask anything about your leads, evidence or actions."));
    const examples = ["Which leads have contradictory evidence?", "What actions are waiting for approval?", "Show me high-confidence opportunities.", "Which claims have only one source?"];
    const exWrap = el("div", { class: "ask-examples" });
    examples.forEach((q) => { const ex = el("div", { class: "ask-example" }, q); ex.addEventListener("click", () => runAsk(q)); exWrap.appendChild(ex); });
    body.appendChild(exWrap);
    const answerWrap = el("div", { id: "askAnswer" });
    body.appendChild(answerWrap);
    const row = el("div", { class: "ask-input-row" });
    const input = el("input", { placeholder: "Ask a question…" });
    const send = el("button", { class: "btn btn-primary btn-sm" }, "Ask");
    row.appendChild(input); row.appendChild(send);
    body.appendChild(row);
    const go = () => { if (input.value.trim()) runAsk(input.value.trim()); };
    send.addEventListener("click", go);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  }
  function closeAsk() { window.__askPanelOpen = false; closePanel(); }

  function runAsk(question) {
    const answerWrap = $("#askAnswer");
    if (!answerWrap) return;
    const q = question.toLowerCase();
    let text = "", links = [];

    if (q.includes("contradict")) {
      const rows = State.overview.filter((r) => r.status === "blocked");
      text = rows.length ? `${rows.length} lead${rows.length === 1 ? "" : "s"} currently ${rows.length === 1 ? "has" : "have"} contradicted or blocked evidence.` : "No leads currently have contradicted evidence.";
      links = rows;
    } else if (q.includes("waiting") || q.includes("pending") || q.includes("approval")) {
      const rows = State.overview.filter((r) => r.action_status === "PENDING_APPROVAL");
      text = rows.length ? `${rows.length} action${rows.length === 1 ? " is" : "s are"} waiting for your approval.` : "Nothing is waiting for approval right now.";
      links = rows;
    } else if (q.includes("high-confidence") || q.includes("high confidence") || q.includes("opportunit")) {
      const rows = State.overview.filter((r) => (r.confidence ?? 0) >= 0.75);
      text = rows.length ? `${rows.length} lead${rows.length === 1 ? "" : "s"} at 75%+ confidence.` : "No leads are currently above 75% confidence.";
      links = rows;
    } else if (q.includes("why") && q.includes("block")) {
      const rows = State.overview.filter((r) => r.status === "blocked");
      text = rows.length ? `${rows[0].name} was blocked because the decision pipeline returned CLOSE or the preflight guardrail flagged the proposed action. Open the record to see the exact reasoning.` : "No blocked leads to explain right now.";
      links = rows.slice(0, 1);
    } else if (q.includes("one source") || q.includes("single source")) {
      text = "Open a lead's Evidence tab and check each claim's source count — this needs per-claim inspection, which isn't summarized here yet.";
    } else {
      const rows = State.overview.filter((r) => r.name.toLowerCase().includes(q));
      text = rows.length ? `Found ${rows.length} matching lead${rows.length === 1 ? "" : "s"}.` : "I can answer questions about confidence, contradictions, and pending approvals — try one of the examples above.";
      links = rows;
    }

    answerWrap.innerHTML = "";
    const card = el("div", { class: "ask-answer fade-in" }, [text]);
    if (links.length) {
      const linkWrap = el("div", { class: "ask-links" });
      links.slice(0, 8).forEach((r) => {
        const chip = el("span", { class: "ask-link" }, r.name);
        chip.addEventListener("click", () => openRecordPanel(r.id));
        linkWrap.appendChild(chip);
      });
      card.appendChild(linkWrap);
    }
    answerWrap.appendChild(card);
  }

  $("#askFab").addEventListener("click", openAsk);

  // ---------------------------------------------------------------
  // Wiring: sidebar nav, collapse, refresh
  // ---------------------------------------------------------------
  $$(".nav-item[data-route]").forEach((n) => n.addEventListener("click", () => navigate(n.dataset.route)));
  $("#collapseBtn").addEventListener("click", () => $("#app").classList.toggle("sidebar-collapsed"));
  $("#refreshBtn").addEventListener("click", async () => {
    await loadOverview({ silent: true });
    render();
    toast("Refreshed");
  });

  // ---------------------------------------------------------------
  // Live Call widget — standalone floating panel, deliberately NOT
  // wired into the router/State machinery above so it can't break the
  // rest of the dashboard. Poll interval is 1s while a call is open
  // (vs. the manual/on-demand fetches elsewhere) since this is the
  // "must not be awkward on stage" surface.
  //
  // LiveCallState is the SINGLE source of truth for live-call data —
  // both the widget's own feed and the table/panel badges read from it.
  // Nothing else runs a second poll loop; applyLiveCallBadges() is just
  // a DOM-sync step called after this poll updates the shared state, and
  // again from render() so badges survive route/table re-renders.
  // ---------------------------------------------------------------
  const LiveCallState = { leadId: null, requests: [] };

  function phoneIconSmall() {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "11"); s.setAttribute("height", "11");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
    s.innerHTML = '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/>';
    return s;
  }

  function chevronIcon() {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "14"); s.setAttribute("height", "14");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
    s.innerHTML = '<path d="M6 9l6 6 6-6"/>';
    return s;
  }

  // Injects/updates a small badge on every [data-lead-id] element matching
  // the lead currently on a live call, wherever it appears in the DOM right
  // now (Leads table rows, Actions work-rows, etc). Called after every
  // poll tick AND from render(), so it stays correct across navigation.
  function applyLiveCallBadges() {
    $$("[data-lead-id]").forEach((rowEl) => {
      const existing = $(".live-call-badge", rowEl);
      if (rowEl.dataset.leadId !== LiveCallState.leadId || !LiveCallState.requests.length) {
        if (existing) existing.remove();
        return;
      }
      const hasPending = LiveCallState.requests.some((r) => r.status === "PENDING_APPROVAL");
      const badge = existing || el("span", { class: "live-call-badge" }, [phoneIconSmall(), el("span", {}, "Live")]);
      badge.classList.toggle("has-pending", hasPending);
      if (!existing) {
        const nameCell = $(".cell-name", rowEl) || $(".work-lead-name", rowEl) || rowEl;
        nameCell.appendChild(badge);
      }
    });
  }

  function mountLiveCallWidget() {
    const box = el("div", { class: "live-call-widget", id: "liveCallWidget" });
    const header = el("div", { class: "live-call-header" }, [
      el("div", { class: "live-call-title" }, [phoneIconSmall(), el("span", {}, "Live Call")]),
    ]);
    const collapseBtn = el("button", { class: "icon-btn", id: "lcwToggle", title: "Collapse" }, [chevronIcon()]);
    header.appendChild(collapseBtn);

    const body = el("div", { class: "live-call-body", id: "lcwBody" });
    const inputsRow = el("div", { class: "live-call-row-inputs" }, [
      el("input", { id: "lcwLeadId", placeholder: "lead id" }),
      el("input", { id: "lcwPhone", placeholder: "+1..." }),
    ]);
    const startBtn = el("button", { class: "btn btn-primary btn-sm", id: "lcwStart", style: "width:100%;margin-bottom:8px;" }, "Start Live Call");
    const status = el("div", { class: "live-call-status", id: "lcwStatus" }, "No active call.");
    const feed = el("div", { class: "live-call-feed", id: "lcwFeed" });
    body.appendChild(inputsRow);
    body.appendChild(startBtn);
    body.appendChild(status);
    body.appendChild(feed);

    box.appendChild(header);
    box.appendChild(body);
    document.body.appendChild(box);

    let pollTimer = null;
    let activeLeadId = null;

    function renderFeed(requests) {
      feed.innerHTML = "";
      for (const r of requests) {
        const isAuto = r.status === "AUTO_EXECUTED";
        const isPending = r.status === "PENDING_APPROVAL";
        const row = el("div", { class: `live-call-entry ${isAuto ? "auto" : isPending ? "pending" : ""}` });
        const head = el("div", { class: "live-call-entry-head" });
        const STATUS_PILLS = {
          AUTO_EXECUTED: ["pill-green", "Auto-executed"],
          PENDING_APPROVAL: ["pill-amber", "Pending approval"],
          APPROVED_EXECUTED: ["pill-green", "Approved"],
          REJECTED: ["pill-neutral", "Rejected"],
        };
        const [pillCls, pillLabel] = STATUS_PILLS[r.status] || ["pill-neutral", r.status];
        head.appendChild(el("span", { class: `pill ${pillCls}` }, [el("span", { class: "pill-dot" }), pillLabel]));
        if (r.risk) head.appendChild(el("span", { class: "pill pill-neutral" }, r.risk));
        row.appendChild(head);
        row.appendChild(el("div", { class: "live-call-transcript" }, `“${r.raw_transcript}”`));
        row.appendChild(el("div", { class: "live-call-reply" }, r.spoken_reply || r.reason || ""));
        if (isPending) {
          const actions = el("div", { class: "live-call-entry-actions" });
          const approve = el("button", { class: "btn btn-primary btn-sm" }, "Approve");
          const reject = el("button", { class: "btn btn-danger btn-sm" }, "Reject");
          approve.addEventListener("click", async () => {
            await api(`/api/live-requests/${r.id}/approve?approved=true`, { method: "POST" });
            poll();
          });
          reject.addEventListener("click", async () => {
            await api(`/api/live-requests/${r.id}/approve?approved=false`, { method: "POST" });
            poll();
          });
          actions.appendChild(approve); actions.appendChild(reject);
          row.appendChild(actions);
        }
        feed.appendChild(row);
      }
    }

    async function poll() {
      if (!activeLeadId) return;
      try {
        const { live_requests } = await api(`/api/leads/${activeLeadId}/live-requests`);
        renderFeed(live_requests);
        status.textContent = `Call active for lead ${activeLeadId} — ${live_requests.length} request(s) so far.`;
        LiveCallState.leadId = activeLeadId;
        LiveCallState.requests = live_requests;
        applyLiveCallBadges();
        // Keep an open record panel for this lead in sync too (the "Live
        // call updates" section reads live-requests the same way).
        if (State.selectedLeadId === activeLeadId && $("#sidePanel").classList.contains("open")) {
          renderLiveCallPanelSection(activeLeadId, live_requests);
        }
      } catch (e) {
        status.textContent = `Poll error: ${e.message}`;
      }
    }

    startBtn.addEventListener("click", async () => {
      const leadId = $("#lcwLeadId", box).value.trim();
      const phone = $("#lcwPhone", box).value.trim();
      if (!leadId || !phone) return toast("Enter a lead id and phone number");
      try {
        await api(`/api/leads/${leadId}/call/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ to_phone_number: phone }),
        });
        activeLeadId = leadId;
        status.textContent = "Call placed — waiting for live requests...";
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(poll, 1000); // 1s: this is the on-stage surface, keep it snappy
        poll();
      } catch (e) {
        toast(`Call failed to start: ${e.message}`);
      }
    });

    collapseBtn.addEventListener("click", () => {
      body.classList.toggle("collapsed");
      collapseBtn.querySelector("svg").style.transform = body.classList.contains("collapsed") ? "rotate(-90deg)" : "";
    });

    // Test/demo-only hook: lets E2E tests (and this screenshot script)
    // exercise the widget's real poll path against data seeded via the
    // /twilio/* webhook simulation, without needing live Twilio
    // credentials to click "Start Live Call". Does not change any
    // production code path - poll() and renderFeed() are the exact same
    // functions a real call would drive.
    window.__lcwDebugPoll = (leadId) => {
      activeLeadId = leadId;
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(poll, 1000);
      return poll();
    };
  }

  // Appends a "Live call updates" section to the currently-open record
  // panel, derived entirely from GET /api/leads/{id}/live-requests — no
  // backend change needed for this. Shows the latest applied value per
  // change_type from AUTO_EXECUTED or human-APPROVED_EXECUTED requests,
  // so approving a held request in the widget visibly updates something
  // real here, not just the request's own status.
  //
  // Chosen fix for "silent no-op on approve" (see README §4): option (a),
  // wired to a real visible field, scoped to this read-only derived view —
  // no new order/schedule backend was built.
  function renderLiveCallPanelSection(leadId, liveRequests) {
    const old = $("#panelLiveCallSection");
    if (old) old.remove();

    const applied = liveRequests.filter((r) => r.status === "AUTO_EXECUTED" || r.status === "APPROVED_EXECUTED");
    if (!applied.length) return;

    const latestByType = {};
    applied.forEach((r) => {
      if (!latestByType[r.change_type] || r.created_at > latestByType[r.change_type].created_at) {
        latestByType[r.change_type] = r;
      }
    });

    const describe = (r) => {
      const cv = r.change_value || {};
      if (r.change_type === "adjust_quantity") return `Quantity: ${cv.new_quantity}`;
      if (r.change_type === "adjust_delivery_date") return `Delivery date shifted by ${cv.shift_days} day(s)`;
      if (r.change_type === "change_followup_time") return `Follow-up shifted by ${cv.shift_days} day(s)`;
      if (r.change_type === "adjust_wording") return `Wording: “${cv.new_text}”`;
      return r.change_type;
    };

    const section = el("div", { id: "panelLiveCallSection" });
    section.appendChild(el("div", { class: "section-label" }, "Live call updates"));
    const grid = el("div", { class: "field-grid live-call-field-grid" });
    Object.values(latestByType).forEach((r) => {
      grid.appendChild(el("div", { class: "field-label" }, r.status === "AUTO_EXECUTED" ? "Auto-executed" : "Human-approved"));
      grid.appendChild(el("div", { class: "field-value" }, describe(r)));
    });
    section.appendChild(grid);
    $("#panelBody").appendChild(section);
  }

  // ---------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------
  (async function boot() {
    const initialRoute = (location.hash || "#home").slice(1).split("?")[0] || "home";
    State.route = ROUTE_TITLES[initialRoute] ? initialRoute : "home";
    render();
    try {
      await loadOverview();
    } catch (e) {
      $("#view").prepend(errorBanner("Couldn't reach ProofFirst backend", e.message));
    }
    render();
    mountLiveCallWidget();
  })();
})();
