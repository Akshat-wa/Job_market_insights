// API base: set window.API_BASE before this script in production, or use localhost for dev.
const API_BASE = window.API_BASE || "http://127.0.0.1:8000";

const queryInput = document.getElementById("queryInput");
const runButton = document.getElementById("runButton");
const summaryBox = document.getElementById("summaryBox");
const tableBox = document.getElementById("tableBox");
const quickChips = document.querySelectorAll(".quick-chip");

const sessionIdEl = document.getElementById("sessionId");
const newSessionBtn = document.getElementById("newSessionBtn");
const dataStatsEl = document.getElementById("dataStats");
const uploadBtn = document.getElementById("uploadBtn");
const loadDemoBtn = document.getElementById("loadDemoBtn");
const usePortfolioBtn = document.getElementById("usePortfolioBtn");
const uploadStatusEl = document.getElementById("uploadStatus");
const combinedFileInput = document.getElementById("combinedFile");
const postingsFileInput = document.getElementById("postingsFile");
const skillsFileInput = document.getElementById("skillsFile");

let sessionId = localStorage.getItem("jmi_session_id") || null;
let isRunning = false;

function apiUrl(path) {
    return `${API_BASE.replace(/\/$/, "")}${path}`;
}

async function fetchWithRetry(url, options = {}, retries = 3, delayMs = 12000) {
    let lastErr;
    for (let i = 0; i < retries; i++) {
        try {
            const res = await fetch(url, options);
            return res;
        } catch (err) {
            lastErr = err;
            if (i < retries - 1) {
                uploadStatusEl.textContent =
                    `Waking API… retry ${i + 2}/${retries} (Render free tier cold start)`;
                await new Promise((r) => setTimeout(r, delayMs));
            }
        }
    }
    throw lastErr;
}

async function ensureSession() {
    if (sessionId) {
        sessionIdEl.textContent = sessionId.slice(0, 8) + "…";
        return sessionId;
    }
    const res = await fetch(apiUrl("/api/session/new"), { method: "POST" });
    if (!res.ok) throw new Error(`Session create failed: HTTP ${res.status}`);
    const data = await res.json();
    sessionId = data.session_id;
    localStorage.setItem("jmi_session_id", sessionId);
    sessionIdEl.textContent = sessionId.slice(0, 8) + "…";
    return sessionId;
}

async function refreshStats() {
    if (!sessionId) return;
    try {
        const res = await fetch(apiUrl(`/api/session/${sessionId}/stats`));
        if (!res.ok) return;
        const data = await res.json();
        dataStatsEl.textContent = `${data.jobs} jobs · ${data.skill_links} skill links in your session`;
        if (sessionId === "demo" && data.jobs < 5000) {
            uploadStatusEl.textContent =
                "⚠ Demo data was overwritten (only " + data.jobs + " jobs). Run: python seed_portfolio.py";
        }
    } catch {
        dataStatsEl.textContent = "Could not load session stats.";
    }
}

function collectTables(structured) {
    const tables = [];
    if (!structured) return tables;

    if (Array.isArray(structured)) {
        if (structured.length && typeof structured[0] === "object") {
            tables.push({ label: "results", rows: structured });
        }
        return tables;
    }

    if (typeof structured === "object") {
        for (const [key, value] of Object.entries(structured)) {
            if (Array.isArray(value)) {
                if (value.length && typeof value[0] === "object") {
                    tables.push({ label: key, rows: value });
                }
            } else if (value && typeof value === "object") {
                tables.push({ label: key, rows: [value] });
            }
        }
    }
    return tables;
}

function buildTable(rows) {
    if (!rows || !rows.length) return null;

    const colsSet = new Set();
    const maxScan = Math.min(rows.length, 20);
    for (let i = 0; i < maxScan; i++) {
        const r = rows[i];
        if (r && typeof r === "object") {
            Object.keys(r).forEach((k) => colsSet.add(k));
        }
    }
    const cols = Array.from(colsSet);
    if (!cols.length) return null;

    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const tbody = document.createElement("tbody");

    const headerRow = document.createElement("tr");
    cols.forEach((c) => {
        const th = document.createElement("th");
        th.textContent = c;
        headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);

    const maxRows = Math.min(rows.length, 100);
    for (let i = 0; i < maxRows; i++) {
        const r = rows[i];
        const tr = document.createElement("tr");
        cols.forEach((c) => {
            const td = document.createElement("td");
            let val = r && Object.prototype.hasOwnProperty.call(r, c) ? r[c] : "";
            if (val === null || val === undefined) val = "";
            if (typeof val === "object") {
                try {
                    val = JSON.stringify(val);
                } catch {
                    val = "[object]";
                }
            }
            td.textContent = String(val);
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    }

    table.appendChild(thead);
    table.appendChild(tbody);
    return table;
}

function renderTable(structured) {
    tableBox.innerHTML = "";
    const tables = collectTables(structured);

    if (!tables.length) {
        const div = document.createElement("div");
        div.className = "placeholder";
        div.textContent = "No tabular structured data returned.";
        tableBox.appendChild(div);
        return;
    }

    tables.forEach(({ label, rows }) => {
        const section = document.createElement("div");
        section.className = "table-section";

        const title = document.createElement("div");
        title.className = "table-section-title";
        title.textContent = label.replace(/_/g, " ");
        section.appendChild(title);

        const table = buildTable(rows);
        if (table) section.appendChild(table);
        tableBox.appendChild(section);
    });
}

async function runQuery(textFromChip) {
    if (isRunning) return;

    const query = (textFromChip ?? queryInput.value).trim();
    if (!query) return;

    isRunning = true;
    runButton.disabled = true;
    const oldLabel = runButton.textContent;
    runButton.textContent = "Running…";

    summaryBox.textContent = "Running query…";
    tableBox.innerHTML = '<div class="placeholder">Fetching data…</div>';

    try {
        await ensureSession();
        const res = await fetchWithRetry(apiUrl("/api/query"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: query, session_id: sessionId }),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

        summaryBox.textContent =
            data.summary ||
            data.answer ||
            "Query completed. No summary returned.";

        renderTable(data.structured_result || data.result || null);
    } catch (err) {
        summaryBox.textContent = "Error while running query.";
        tableBox.innerHTML = "";
        const div = document.createElement("div");
        div.className = "placeholder";
        div.textContent = String(err);
        tableBox.appendChild(div);
    } finally {
        isRunning = false;
        runButton.disabled = false;
        runButton.textContent = oldLabel;
    }
}

async function uploadFiles({ combined, postings, skills, demo = false }) {
    if (isRunning) return;
    isRunning = true;
    uploadBtn.disabled = true;
    loadDemoBtn.disabled = true;
    usePortfolioBtn.disabled = true;
    uploadStatusEl.textContent = demo ? "Loading sample data…" : "Uploading…";

    try {
        await ensureSession();
        const form = new FormData();
        form.append("session_id", sessionId);
        form.append("mode", "replace");

        if (demo) {
            const res = await fetch("../demo/sample_combined.csv");
            if (!res.ok) throw new Error("Could not fetch sample CSV");
            const blob = await res.blob();
            form.append("combined_file", blob, "sample_combined.csv");
        } else {
            if (combined) form.append("combined_file", combined);
            if (postings) form.append("postings_file", postings);
            if (skills) form.append("skills_file", skills);
        }

        const up = await fetch(apiUrl("/api/upload"), { method: "POST", body: form });
        const data = await up.json();
        if (!up.ok) throw new Error(data.error || `Upload failed HTTP ${up.status}`);

        const warnings = (data.warnings || []).join(" · ");
        const stats = data.stats || {};
        uploadStatusEl.textContent =
            `Ingested ${stats.jobs_inserted ?? "?"} jobs` +
            (warnings ? ` — ${warnings}` : "");
        await refreshStats();
    } catch (err) {
        uploadStatusEl.textContent = String(err);
    } finally {
        isRunning = false;
        uploadBtn.disabled = false;
        loadDemoBtn.disabled = false;
        usePortfolioBtn.disabled = false;
    }
}

uploadBtn.addEventListener("click", () => {
    const combined = combinedFileInput.files[0] || null;
    const postings = postingsFileInput.files[0] || null;
    const skills = skillsFileInput.files[0] || null;
    if (!combined && !postings && !skills) {
        uploadStatusEl.textContent = "Choose at least one CSV file.";
        return;
    }
    uploadFiles({ combined, postings, skills });
});

async function loadDemoData({ usePortfolio = false, targetSession = null } = {}) {
    if (isRunning) return;
    isRunning = true;
    uploadBtn.disabled = true;
    loadDemoBtn.disabled = true;
    usePortfolioBtn.disabled = true;
    uploadStatusEl.textContent = usePortfolio
        ? "Attaching portfolio demo session…"
        : "Loading small sample…";

    try {
        if (usePortfolio) {
            await useDemoSession();
            uploadStatusEl.textContent = "Using portfolio demo session.";
            return;
        }

        await ensureSession();
        if (sessionId === "demo") {
            sessionId = null;
            localStorage.removeItem("jmi_session_id");
            await ensureSession();
        }
        const res = await fetch(apiUrl("/api/load-demo"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId, use_portfolio: false }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Demo load failed");
        uploadStatusEl.textContent = `Loaded ${data.stats?.jobs_inserted ?? "?"} sample jobs`;
        await refreshStats();
    } catch (err) {
        uploadStatusEl.textContent = String(err);
    } finally {
        isRunning = false;
        uploadBtn.disabled = false;
        loadDemoBtn.disabled = false;
        usePortfolioBtn.disabled = false;
    }
}

loadDemoBtn.addEventListener("click", () => loadDemoData({ usePortfolio: false }));
usePortfolioBtn.addEventListener("click", () => loadDemoData({ usePortfolio: true, targetSession: "demo" }));

newSessionBtn.addEventListener("click", async () => {
    sessionId = null;
    localStorage.removeItem("jmi_session_id");
    await ensureSession();
    dataStatsEl.textContent = "New session — upload data to begin.";
    uploadStatusEl.textContent = "";
});

runButton.addEventListener("click", () => runQuery());
queryInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runQuery();
});
quickChips.forEach((chip) => {
    chip.addEventListener("click", () => {
        if (isRunning) return;
        const q = chip.dataset.query || chip.textContent;
        queryInput.value = q;
        runQuery(q);
    });
});

async function tryDemoSession() {
    try {
        const res = await fetchWithRetry(apiUrl("/api/session/demo/stats"));
        if (!res.ok) return false;
        const data = await res.json();
        return (data.jobs || 0) >= 5000;
    } catch {
        return false;
    }
}

async function useDemoSession() {
    sessionId = "demo";
    localStorage.setItem("jmi_session_id", sessionId);
    sessionIdEl.textContent = "demo (portfolio)";
    await refreshStats();
}

(async function init() {
    try {
        const hasDemo = await tryDemoSession();
        if (hasDemo) {
            await useDemoSession();
        } else {
            await ensureSession();
            await refreshStats();
            uploadStatusEl.textContent =
                "Demo session empty or overwritten. Run: python seed_portfolio.py then refresh.";
        }
    } catch (err) {
        sessionIdEl.textContent = "offline";
        dataStatsEl.textContent = `API not reachable at ${API_BASE}. Wait ~60s and refresh (Render cold start).`;
        uploadStatusEl.textContent = String(err);
    }
})();
