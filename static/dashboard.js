
let latencyChart = null;

let speedtestChart = null;

let latencyChartType = "line";

let speedtestChartType = "line";

let lastLatencyData = null;

let lastSpeedtestData = null;

let currentCompany =
    document.getElementById("companySelect")
        ? document.getElementById("companySelect").value
        : "";


/* ============================================================
   HELPERS
============================================================ */

function byId(id) {

    return document.getElementById(id);

}

let rootStyle = getComputedStyle(document.documentElement);

function token(name, fallback) {

    return rootStyle.getPropertyValue(name).trim() || fallback;

}

function escapeHtml(value) {

    return String(value).replace(
        /[&<>"']/g,
        function(character) {

            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;"
            }[character];

        }
    );

}

function formatNumber(value) {

    const number = Number(value);

    if (!isFinite(number)) {

        return String(value);

    }

    return String(parseFloat(number.toFixed(1)));

}

function withUnit(value, unit) {

    if (value === null || value === undefined) {

        return "--";

    }

    return (
        escapeHtml(value)
        + ' <span class="unit">'
        + unit
        + "</span>"
    );

}

function setText(element, text) {

    if (element && element.textContent !== text) {

        element.textContent = text;

    }

}


/* ============================================================
   TIMEZONE
   The database + API store timestamps in UTC ("YYYY-MM-DD
   HH:MM:SS"). Browsers render them here in the viewer's LOCAL
   timezone (e.g. UTC+8 Kuala Lumpur/Singapore), so the charts
   and tables match the wall clock. Backend stays UTC.
============================================================ */

function parseUtcTimestamp(value) {

    if (!value) {

        return null;

    }

    const date = new Date(
        String(value).replace(" ", "T") + "Z"
    );

    return isNaN(date.getTime()) ? null : date;

}


function pad2(number) {

    return String(number).padStart(2, "0");

}


function formatUtcToLocal(value) {

    const date = parseUtcTimestamp(value);

    if (!date) {

        return value;

    }

    return (
        date.getFullYear() + "-"
        + pad2(date.getMonth() + 1) + "-"
        + pad2(date.getDate()) + " "
        + pad2(date.getHours()) + ":"
        + pad2(date.getMinutes()) + ":"
        + pad2(date.getSeconds())
    );

}


/* ============================================================
   COMPANY SWITCHER
============================================================ */

function handleCompanyChange(value) {

    currentCompany = value;

    const range =
        byId("latencyRange").value;

    /* Keep the URL shareable / back-button friendly. */
    try {
        const url = new URL(window.location.href);
        url.searchParams.set("company", currentCompany);
        history.replaceState(null, "", url);
    } catch (e) { /* ignore */ }

    setStale(true);

    setOverall(
        "loading",
        "Checking connection",
        "Loading results for " + currentCompany + "."
    );

    loadStatus();

    loadUptime(range);

    loadLatency(
        range,
        byId("latencyResolution") && byId("latencyResolution").value
    );

    loadSpeedtest(
        byId("speedtestRange").value,
        byId("speedtestResolution") && byId("speedtestResolution").value
    );

    loadIncidents();

}


function companyQuery() {

    return (
        "company="
        + encodeURIComponent(currentCompany)
    );

}


/* ============================================================
   SIDEBAR NAVIGATION
============================================================ */

function setActiveNav(element) {

    document
        .querySelectorAll(".nav-item")
        .forEach(
            function(item) {

                item.classList.remove("active");

            }
        );

    element.classList.add("active");

    closeNav();

}


/* Mobile drawer */

function openNav() {

    document.body.classList.add("nav-open");

    byId("menuButton").setAttribute("aria-expanded", "true");

}

function closeNav() {

    document.body.classList.remove("nav-open");

    byId("menuButton").setAttribute("aria-expanded", "false");

}

byId("menuButton").addEventListener(
    "click",
    function() {

        if (document.body.classList.contains("nav-open")) {

            closeNav();

        }

        else {

            openNav();

        }

    }
);

byId("scrim").addEventListener("click", closeNav);

document.addEventListener(
    "keydown",
    function(event) {

        if (event.key === "Escape") {

            closeNav();

        }

    }
);


/* ============================================================
   NETWORK STATUS
============================================================ */

const CHECK_ORDER = [
    "Gateway",
    "Internet",
    "DNS",
    "HTTPS"
];

const CHECKS = {

    Gateway: {
        status: "gatewayStatus",
        latency: "gatewayLatency",
        target: "gatewayTarget",
        uptime: "gatewayUptime"
    },

    Internet: {
        status: "internetStatus",
        latency: "internetLatency",
        target: "internetTarget",
        uptime: "internetUptime"
    },

    DNS: {
        status: "dnsStatus",
        latency: "dnsLatency",
        target: "dnsTarget",
        uptime: "dnsUptime"
    },

    HTTPS: {
        status: "httpsStatus",
        latency: "httpsLatency",
        target: "httpsTarget",
        uptime: "httpsUptime"
    }

};


function renderCheck(type, result) {

    const ids = CHECKS[type];

    const statusElement = byId(ids.status);

    const latencyElement = byId(ids.latency);

    const targetElement = byId(ids.target);

    const card = statusElement.closest(".status-card");


    /* No row recorded yet for this check */

    if (!result) {

        statusElement.textContent = "No data";

        statusElement.className = "status-value";

        latencyElement.textContent = "--";

        targetElement.textContent = "--";

        targetElement.removeAttribute("title");

        card.dataset.state = "idle";

        return;

    }


    const isUp = result.status === "UP";

    statusElement.textContent = result.status;

    statusElement.className =
        "status-value "
        + (isUp ? "online" : "offline");

    card.dataset.state = isUp ? "up" : "down";


    if (
        result.latency !== null
        &&
        result.latency !== undefined
    ) {

        latencyElement.textContent =
            formatNumber(result.latency) + " ms";

    }

    else {

        latencyElement.textContent =
            isUp ? "--" : "No reply";

    }


    targetElement.textContent = result.target || "--";

    if (result.target) {

        targetElement.title = result.target;

    }

    else {

        targetElement.removeAttribute("title");

    }

}


/* Headline: the overall state of the selected site */

function setOverall(state, title, detail) {

    const box = byId("overall");

    if (box.dataset.state !== state) {

        box.dataset.state = state;

    }

    setText(byId("overallTitle"), title);

    setText(byId("overallDetail"), detail);

}


function joinNames(names) {

    if (names.length <= 1) {

        return names.join("");

    }

    return (
        names.slice(0, -1).join(", ")
        + " and "
        + names[names.length - 1]
    );

}


function updateOverall(results) {

    const checks = results.filter(
        function(result) {

            return Boolean(CHECKS[result.check_type]);

        }
    );

    const total = checks.length;

    if (total === 0) {

        setOverall(
            "idle",
            "No data yet",
            "No checks have been recorded for "
                + currentCompany + " yet."
        );

        return;

    }

    const failing = CHECK_ORDER.filter(
        function(type) {

            return checks.some(
                function(result) {

                    return (
                        result.check_type === type
                        &&
                        result.status !== "UP"
                    );

                }
            );

        }
    );

    if (failing.length === 0) {

        setOverall(
            "ok",
            "All checks passing",
            "Every check is responding for "
                + currentCompany + "."
        );

    }

    else if (failing.length === total) {

        setOverall(
            "down",
            "Network is down",
            "No check is responding for "
                + currentCompany + "."
        );

    }

    else {

        setOverall(
            "degraded",
            joinNames(failing)
                + (failing.length === 1 ? " is" : " are")
                + " failing",
            (total - failing.length)
                + " of " + total
                + " checks are passing for "
                + currentCompany + "."
        );

    }

}


function setStale(isStale) {

    document
        .querySelector(".status-grid")
        .classList.toggle("is-stale", isStale);

}


function setLive(isLive) {

    setStale(!isLive);

    byId("refreshInfo").dataset.state =
        isLive ? "live" : "error";

    setText(
        byId("lastUpdated"),
        isLive
            ? "Updated " + new Date().toLocaleTimeString(
                [],
                {
                    hour: "numeric",
                    minute: "2-digit",
                    second: "2-digit"
                }
            )
            : "Connection lost. Retrying..."
    );

}


async function loadStatus() {

    try {

        const response =
            await fetch(
                "/api/status?" + companyQuery()
            );

        if (!response.ok) {

            throw new Error(
                "Unable to load status"
            );

        }

        const data =
            await response.json();

        const byType = {};

        data.results.forEach(
            function(result) {

                byType[result.check_type] = result;

            }
        );

        CHECK_ORDER.forEach(
            function(type) {

                renderCheck(
                    type,
                    byType[type] || null
                );

            }
        );

        updateOverall(data.results);

        /* Remote-agent staleness badge (managed_by: agent only). */
        try {
            const badge = document.getElementById("agentBadge");
            const badgeText = document.getElementById("agentBadgeText");
            if (badge && data.agent) {
                badge.hidden = false;
                badge.classList.toggle("agent-badge-stale", !!data.agent.stale);
                if (!data.agent.last_seen) {
                    badgeText.textContent =
                        "Remote agent has never reported. Start agent.py at the site.";
                } else if (data.agent.stale) {
                    const mins = Math.floor(data.agent.age_seconds / 60);
                    badgeText.textContent =
                        "Remote agent data is stale - last report " +
                        mins + " min ago. Is agent.py running?";
                } else {
                    badgeText.textContent =
                        "Remote agent - last report " +
                        data.agent.age_seconds + "s ago.";
                }
            } else if (badge) {
                badge.hidden = true;
            }
        } catch (e) { /* badge is cosmetic - never break the page */ }

        /* Stale-data guard: if the newest check is >3 min old,
           the collector may be stopped - flag it. */
        try {
            let newest = 0;
            data.results.forEach(function(r) {
                if (r.timestamp) {
                    const t = new Date(r.timestamp.replace(" ", "T") + "Z").getTime();
                    if (isFinite(t) && t > newest) { newest = t; }
                }
            });
            if (newest && (Date.now() - newest > 3 * 60 * 1000)) {
                setLive(false);
                setOverall(
                    "idle",
                    "Data is stale",
                    "Last check was " + new Date(newest).toLocaleTimeString() +
                    ". The monitor may be stopped."
                );
                return;
            }
        } catch (e) { /* clock parse never blocks status */ }

        setLive(true);

    }

    catch (error) {

        console.error(
            "Status error:",
            error
        );

        setLive(false);

        setOverall(
            "idle",
            "Can't load status",
            "The dashboard can't reach the monitor. It will keep trying."
        );

    }

}


/* ============================================================
   UPTIME PERCENTAGE (last 24 hours)
============================================================ */

async function loadUptime(hours) {

    const rangeHours = hours || 24;

    try {

        const response =
            await fetch(
                "/api/uptime?hours=" + rangeHours + "&" + companyQuery()
            );

        if (!response.ok) {

            throw new Error(
                "Unable to load uptime"
            );

        }

        const data =
            await response.json();

        CHECK_ORDER.forEach(
            function(type) {

                const element =
                    byId(CHECKS[type].uptime);

                if (!element) {

                    return;

                }

                const percent =
                    data.uptime[type];

                element.classList.remove(
                    "good",
                    "warn",
                    "bad"
                );

                if (
                    percent === undefined
                    ||
                    percent === null
                ) {

                    element.textContent = "--";

                    return;

                }

                element.textContent =
                    percent + "%";

                if (percent >= 99) {

                    element.classList.add("good");

                }

                else if (percent >= 95) {

                    element.classList.add("warn");

                }

                else {

                    element.classList.add("bad");

                }

            }
        );

        /* Keep the "Uptime, 24h" captions in sync with the range. */
        const labelMap = {1: "Uptime, 1h", 12: "Uptime, 12h", 24: "Uptime, 24h", 720: "Uptime, 30d"};
        const caption = labelMap[rangeHours] || ("Uptime, " + rangeHours + "h");
        document.querySelectorAll(".status-metric .metric-caption").forEach(function(el) {
            if (el.textContent.indexOf("Uptime") === 0) {
                el.textContent = caption;
            }
        });

    }

    catch (error) {

        console.error(
            "Uptime error:",
            error
        );

    }

}


/* ============================================================
   CHART THEME
   Series colours come from the CSS tokens, so the legend keys
   on the status cards and the chart lines always match.
============================================================ */

if (typeof Chart !== "undefined") {

    Chart.defaults.font.family =
        getComputedStyle(document.body).fontFamily;

    Chart.defaults.font.size = 12.5;

    Chart.defaults.color =
        token("--ink-3", "#66727f");

    Chart.defaults.borderColor =
        token("--line", "#e1e6ec");

}


const CHART_COLORS = {

    Gateway: token("--c-gateway", "#4056d6"),
    Internet: token("--c-internet", "#0aa5b5"),
    DNS: token("--c-dns", "#8a5bd8"),
    HTTPS: token("--c-https", "#e58a1f"),
    Download: token("--c-gateway", "#4056d6"),
    Upload: token("--c-internet", "#0aa5b5")

};

function buildDataset(label, values, type) {

    const color = CHART_COLORS[label] || "#6b7280";

    const base = {

        label: label,

        data: values,

        borderColor: color,

        backgroundColor:
            type === "bar"
                ? color + "cc"
                : color + "22",

        spanGaps: false

    };

    if (type === "bar") {

        base.borderWidth = 0;

        base.borderRadius = 4;

        base.maxBarThickness = 28;

    }

    else {

        base.borderWidth = 2.25;

        base.tension = 0.3;

        base.fill = false;

        base.borderJoinStyle = "round";

        /* Dots stay hidden until the cursor is actually over
           that point - keeps the line clean, but a wide
           pointHitRadius means you don't have to land exactly
           on the dot to see it and its value. */
        base.pointRadius = 0;

        base.pointHoverRadius = 5;

        base.pointHitRadius = 14;

        base.pointBackgroundColor = color;

        base.pointBorderColor = "#ffffff";

        base.pointBorderWidth = 2;

    }

    return base;

}


function tooltipColors() {

    /* The tooltip box flips with the theme. In light mode it is the
       dark ink box with white text. In dark mode it must invert to
       the surface box with ink text - the old code bound the
       background to --ink in BOTH themes, so dark mode painted a
       near-white box while the text stayed white: white-on-white
       and completely unreadable. Charts rebuild on theme toggle,
       so these tokens are re-read on every render. */
    const dark =
        document
            .documentElement
            .getAttribute("data-theme") === "dark";

    if (dark) {

        return {
            backgroundColor: token("--surface", "#161d26"),
            titleColor: token("--ink", "#e8edf3"),
            bodyColor: token("--ink-2", "#aab6c3"),
            borderColor: token("--line-2", "#354355"),
            borderWidth: 1
        };

    }

    return {
        backgroundColor: token("--ink", "#12202e"),
        titleColor: "#ffffff",
        bodyColor: "#dfe5ec",
        borderColor: "transparent",
        borderWidth: 0
    };

}


function chartTypeOptions(unit) {

    const tt = tooltipColors();

    return {

        responsive: true,

        maintainAspectRatio: false,

        animation: false,

        interaction: {

            /* Hovering anywhere near a column (not just exactly
               on a dot) reveals that column's points + tooltip -
               this is what makes "hover to see the dots" work. */
            mode: "index",

            intersect: false

        },

        plugins: {

            legend: {

                display: true,

                position: "top",

                align: "end",

                labels: {

                    usePointStyle: true,

                    pointStyle: "circle",

                    boxWidth: 8,

                    boxHeight: 8,

                    padding: 18

                }

            },

            tooltip: {

                mode: "index",

                intersect: false,

                backgroundColor: tt.backgroundColor,

                titleColor: tt.titleColor,

                bodyColor: tt.bodyColor,

                borderColor: tt.borderColor,

                borderWidth: tt.borderWidth,

                padding: 12,

                cornerRadius: 10,

                boxPadding: 6,

                usePointStyle: true,

                callbacks: {

                    label: function(context) {

                        // Downtime must be explicit: a null gap alone is
                        // invisible at day scale, so label it DOWN.
                        if (
                            context.parsed.y === null
                            ||
                            context.parsed.y === undefined
                        ) {

                            return (
                                " " + context.dataset.label
                                + ": DOWN - No reply"
                            );

                        }

                        return (
                            " " + context.dataset.label
                            + ": " + context.parsed.y
                            + " " + unit
                        );

                    },

                    afterBody: function(items) {

                        if (!items || !items.length) {
                            return null;
                        }

                        const idx = items[0].dataIndex;
                        const detail =
                            lastLatencyData
                            && lastLatencyData.down_detail
                            && lastLatencyData.down_detail[idx];

                        if (detail) {
                            return "Downtime: " + detail;
                        }

                        return null;

                    }

                }

            }

        },

        scales: {

            x: {

                grid: {

                    display: false

                },

                ticks: {

                    autoSkip: true,

                    maxTicksLimit: 10,

                    maxRotation: 0,

                    padding: 8

                }

            },

            y: {

                beginAtZero: true,

                border: {

                    display: false

                },

                ticks: {

                    padding: 10,

                    callback: function(value) {

                        return value + " " + unit;

                    }

                }

            }

        }

    };

}


/* ============================================================
   LATENCY
   Buckets average UP replies; any failed check in a bucket
   returns null so the line cuts and resumes on recovery
   (spanGaps:false).
============================================================ */

function latencyResolution() {

    const el = byId("latencyResolution") || byId("latencyBucket");

    return el && el.value ? el.value : "auto";

}


async function loadLatency(hours, resolution) {

    loadUptime(hours);

    var res = resolution || latencyResolution() || "auto";

    // Raw 30-day view would plot tens of thousands of points.
    // Coerce to hourly averages unless the user explicitly wants raw.
    if (String(hours) === "720" && res === "raw") {
        res = "hour";
        if (byId("latencyResolution")) {
            byId("latencyResolution").value = "hour";
        }
    }

    try {

        const response =
            await fetch(
                `/api/latency?hours=${hours}&resolution=${encodeURIComponent(res)}&`
                + companyQuery()
            );

        if (!response.ok) {

            throw new Error(
                "Unable to load latency"
            );

        }

        lastLatencyData =
            await response.json();

        renderLatencyChart();

    }

    catch (error) {

        console.error(
            "Latency graph error:",
            error
        );

    }

}


function handleLatencyRange(value) {

    loadLatency(value, latencyResolution());

}


function handleLatencyResolution(value) {

    loadLatency(
        byId("latencyRange").value,
        value
    );

}


function handleLatencyBucket(value) {

    handleLatencyResolution(value);

}

}


function setLatencyChartType(type) {

    latencyChartType = type;
    document
        .querySelectorAll(
            "#latencyChartToggle button"
        )
        .forEach(function(button) {

            var on = button.dataset.type === type;
            button.classList.toggle("active", on);
            button.setAttribute("aria-pressed", on ? "true" : "false");

        });

    renderLatencyChart();

}


function renderLatencyChart() {

    if (!lastLatencyData) {

        return;

    }

    const data = lastLatencyData;

    if (latencyChart) {

        latencyChart.destroy();

    }

    const canvas =
        byId("latencyChart");

    const ctx =
        canvas.getContext("2d");

    latencyChart =
        new Chart(
            ctx,
            {

                type: latencyChartType,

                data: {

                    labels: data.labels.map(formatUtcToLocal),

                    datasets: [

                        buildDataset(
                            "Gateway",
                            data.gateway,
                            latencyChartType
                        ),

                        buildDataset(
                            "Internet",
                            data.internet,
                            latencyChartType
                        ),

                        buildDataset(
                            "DNS",
                            data.dns,
                            latencyChartType
                        ),

                        buildDataset(
                            "HTTPS",
                            data.https,
                            latencyChartType
                        )

                    ]

                },

                options:
                    chartTypeOptions("ms")

            }
        );

}


/* ============================================================
   SPEEDTEST
============================================================ */

function lastValue(values) {

    const valid = values.filter(
        function(value) {

            return (
                value !== null
                &&
                value !== undefined
            );

        }
    );

    return valid.length > 0
        ? valid[valid.length - 1]
        : null;

}


async function loadSpeedtest(hours, resolution) {

    try {

        var res = resolution
            || (byId("speedtestResolution") && byId("speedtestResolution").value)
            || "raw";

        const response =
            await fetch(
                `/api/speedtest?hours=${hours}&resolution=${encodeURIComponent(res)}&`
                + companyQuery()
            );

        if (!response.ok) {

            throw new Error(
                "Unable to load speedtest"
            );

        }

        lastSpeedtestData =
            await response.json();

        updateSpeedtestCompanyNote(
            lastSpeedtestData.company
        );

        /* ----------------------------------------------------
           CURRENT DOWNLOAD / UPLOAD / PING
        ---------------------------------------------------- */

        const download =
            lastValue(lastSpeedtestData.download);

        if (download !== null) {

            byId("currentDownload").textContent =
                download;

        }

        const upload =
            lastValue(lastSpeedtestData.upload);

        if (upload !== null) {

            byId("currentUpload").textContent =
                upload;

        }

        const ping =
            lastValue(lastSpeedtestData.ping);

        if (ping !== null) {

            byId("currentPing").textContent =
                ping;

        }

        renderSpeedtestChart();

        /* ----------------------------------------------------
           RECENT TABLE
        ---------------------------------------------------- */

        const table =
            byId("speedtestTableBody");

        const recentResponse =
            await fetch(
                "/api/speedtest/recent"
            );

        if (!recentResponse.ok) {

            throw new Error(
                "Unable to load recent results"
            );

        }

        const recentData =
            await recentResponse.json();

        table.innerHTML = "";

        if (
            !recentData.results
            ||
            recentData.results.length === 0
        ) {

            table.innerHTML =
                `
                <tr>
                    <td colspan="5" class="table-message">
                        No speedtest results yet
                    </td>
                </tr>
                `;

            return;

        }

        recentData.results.forEach(
            function(result) {

                const row =
                    document.createElement("tr");

                row.innerHTML = `
                    <td title="${escapeHtml(result.timestamp)} UTC">${escapeHtml(formatUtcToLocal(result.timestamp))}</td>
                    <td class="num">${withUnit(result.download, "Mbps")}</td>
                    <td class="num">${withUnit(result.upload, "Mbps")}</td>
                    <td class="num">${withUnit(result.ping, "ms")}</td>
                    <td class="server" title="${escapeHtml(result.server || "")}">${escapeHtml(result.server || "--")}</td>
                `;

                table.appendChild(row);

            }
        );

    }

    catch (error) {

        console.error(
            "Speedtest error:",
            error
        );

    }

}


function setSpeedtestChartType(type) {

    speedtestChartType = type;

    document
        .querySelectorAll(
            "#speedtestChartToggle button"
        )
        .forEach(function(button) {

            var on = button.dataset.type === type;
            button.classList.toggle("active", on);
            button.setAttribute("aria-pressed", on ? "true" : "false");

        });

    renderSpeedtestChart();

}


function renderSpeedtestChart() {

    if (!lastSpeedtestData) {

        return;

    }

    const data = lastSpeedtestData;

    if (speedtestChart) {

        speedtestChart.destroy();

    }

    const canvas =
        byId("speedtestChart");

    const ctx =
        canvas.getContext("2d");

    speedtestChart =
        new Chart(
            ctx,
            {

                type: speedtestChartType,

                data: {

                    labels: data.labels.map(formatUtcToLocal),

                    datasets: [

                        buildDataset(
                            "Download",
                            data.download,
                            speedtestChartType
                        ),

                        buildDataset(
                            "Upload",
                            data.upload,
                            speedtestChartType
                        )

                    ]

                },

                options:
                    chartTypeOptions("Mbps")

            }
        );

}


function updateSpeedtestCompanyNote(speedtestCompany) {

    const note =
        byId("speedtestCompanyNote");

    if (!note) {

        return;

    }

    if (speedtestCompany === currentCompany) {

        note.style.display = "none";

        return;

    }

    note.style.display = "inline-block";

    note.textContent =
        "Speedtest reflects this machine's own connection "
        + `(tagged "${speedtestCompany}"), not `
        + `"${currentCompany}" specifically.`;

}


/* ============================================================
   INCIDENTS (banner + history table)
============================================================ */

function durationLabel(seconds) {

    if (seconds === null || seconds === undefined) {
        return "open";
    }

    if (seconds < 60) {
        return Math.round(seconds) + "s";
    }
    if (seconds < 3600) {
        return (seconds / 60).toFixed(1) + "m";
    }
    return (seconds / 3600).toFixed(1) + "h";
}


async function loadIncidents() {

    try {

        const response = await fetch(
            "/api/incidents?hours=24&" + companyQuery()
        );

        if (!response.ok) {
            throw new Error("Unable to load incidents");
        }

        const data = await response.json();

        /* --- Banner: only currently open incidents (grouped) --- */

        const open = data.incidents.filter(function(i){ return i.open; });
        const banner = byId("incidentBanner");

        if (open.length) {

            byId("incidentTitle").textContent =
                open.length === 1
                    ? "1 active incident"
                    : open.length + " active incidents";

            const list = byId("incidentList");
            list.innerHTML = "";

            open.forEach(function(i) {

                const li = document.createElement("li");
                li.textContent =
                    i.checks.join(", ") + " down since " + formatUtcToLocal(i.started_at);
                li.title = i.started_at + " UTC";
                list.appendChild(li);

            });

            banner.hidden = false;

        } else {

            banner.hidden = true;

        }

        /* --- History table: grouped incidents (open + closed) --- */

        const table = byId("incidentTableBody");
        table.innerHTML = "";

        if (!data.incidents.length) {

            table.innerHTML =
                '<tr><td colspan="5" class="table-message">' +
                "No incidents in the last 24 hours" +
                "</td></tr>";
            return;

        }

        data.incidents.forEach(function(i) {

            const row = document.createElement("tr");

            row.innerHTML =
                "<td>" + escapeHtml(i.checks.join(", ")) +
                    (i.outage_count > 1
                        ? " (" + i.outage_count + " outages)"
                        : "") + "</td>" +
                "<td title=\"" + escapeHtml(i.started_at) + " UTC\">" + escapeHtml(formatUtcToLocal(i.started_at)) + "</td>" +
                "<td>" + escapeHtml(i.ended_at ? formatUtcToLocal(i.ended_at) : "ongoing") + "</td>" +
                '<td class="num">' +
                    escapeHtml(durationLabel(i.duration)) +
                "</td>" +
                "<td>" + escapeHtml(i.root_cause) + "</td>";

            table.appendChild(row);

        });

    } catch (error) {

        console.error("Incident error:", error);

    }

}


function showIncidentHistory(event) {

    if (event) {
        event.preventDefault();
    }

    const card = byId("incidentHistoryCard");

    if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "start" });
    }

}


/* ============================================================
   TOASTS
   Polls recent notifications and pops a toast for anything
   new since page load. Simple + durable (no websocket needed).
============================================================ */

let lastNotificationId = 0;


function pushToast(kind, message) {

    const stack = byId("toastStack");

    if (!stack) {
        return;
    }

    const toast = document.createElement("div");

    toast.className = "toast toast-" + kind;

    toast.innerHTML =
        '<span class="toast-dot"></span>' +
        '<span class="toast-msg">' + escapeHtml(message) + "</span>" +
        '<button class="toast-close" aria-label="Dismiss">×</button>';

    toast.querySelector(".toast-close").addEventListener(
        "click",
        function() {
            toast.remove();
        }
    );

    stack.appendChild(toast);

    /* Auto-dismiss after 8s. */
    setTimeout(function() {
        toast.classList.add("toast-out");
        setTimeout(function() { toast.remove(); }, 300);
    }, 8000);

    /* Cap stack at 4. */
    while (stack.children.length > 4) {
        stack.removeChild(stack.firstChild);
    }

}


async function pollNotifications() {

    try {

        const response = await fetch("/api/notifications?limit=10");

        if (!response.ok) {
            return;
        }

        const data = await response.json();

        if (!lastNotificationId) {

            /* First poll: baseline, don't toast history. */
            if (data.notifications.length) {
                lastNotificationId = data.notifications[0].id;
            }
            return;

        }

        /* Newest-first list: anything above baseline is new. */
        const fresh = [];

        for (const n of data.notifications) {

            if (n.id > lastNotificationId) {
                fresh.push(n);
            } else {
                break;
            }

        }

        /* Reverse so oldest-new toast appears first. */
        fresh.reverse().forEach(function(n) {

            const kind =
                n.kind === "recovery"
                    ? "ok"
                    : n.kind === "test"
                        ? "info"
                        : "bad";

            pushToast(
                kind,
                n.company + " / " + n.check_type + ": " + n.message
            );

        });

        if (fresh.length) {
            lastNotificationId = fresh[fresh.length - 1].id;
        }

    } catch (error) { /* transient - next poll will retry */ }

}


function initToasts() {

    pollNotifications();

    setInterval(pollNotifications, 30000);

}


/* ============================================================
   DARK MODE
============================================================ */

function applyTheme(theme) {

    document.documentElement.setAttribute("data-theme", theme);

    const btn = byId("themeToggle");
    const label = byId("themeToggleLabel");

    if (btn) {
        btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }

    if (label) {
        label.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    }

    try {
        localStorage.setItem("theme", theme);
    } catch (e) { /* ignore */ }

    /* Re-read CSS tokens + repaint charts in new palette. */
    rootStyle = getComputedStyle(document.documentElement);

    if (typeof Chart !== "undefined") {

        Chart.defaults.color = token("--ink-3", "#66727f");
        Chart.defaults.borderColor = token("--line", "#e1e6ec");

        CHART_COLORS.Gateway = token("--c-gateway", "#4056d6");
        CHART_COLORS.Internet = token("--c-internet", "#0aa5b5");
        CHART_COLORS.DNS = token("--c-dns", "#8a5bd8");
        CHART_COLORS.HTTPS = token("--c-https", "#e58a1f");
        CHART_COLORS.Download = token("--c-gateway", "#4056d6");
        CHART_COLORS.Upload = token("--c-internet", "#0aa5b5");

        if (lastLatencyData) {
            renderLatencyChart();
        }
        if (lastSpeedtestData) {
            renderSpeedtestChart();
        }

    }

}


function toggleTheme() {

    const current =
        document.documentElement.getAttribute("data-theme") || "light";

    applyTheme(current === "dark" ? "light" : "dark");

}


/* Restore saved theme before first paint of charts. */
(function initTheme() {

    let saved = null;

    try {
        saved = localStorage.getItem("theme");
    } catch (e) { /* ignore */ }

    if (!saved) {
        saved = window.matchMedia
        && window.matchMedia("(prefers-color-scheme: dark)").matches
            ? "dark"
            : "light";
    }

    applyTheme(saved);

})();


/* ============================================================
   INITIAL LOAD
============================================================ */

/* Company deep-link: ?company= wins (shareable URL), then the
   sessionStorage hand-off from /overview. */
try {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("company");
    const fromStore = sessionStorage.getItem("ov_company");
    const wanted = fromUrl || fromStore;
    if (wanted) {
        const sel = byId("companySelect");
        const exists = Array.from(sel.options).some(function(o){ return o.value === wanted; });
        if (exists) {
            sel.value = wanted;
            currentCompany = wanted;
        }
        sessionStorage.removeItem("ov_company");
    }
} catch (e) { /* private mode - ignore */ }

loadStatus();

loadLatency(24);

loadSpeedtest(24);

loadIncidents();

initToasts();


/* ============================================================
   AUTO REFRESH
============================================================ */

/*
    Network status:
    Refresh every 30 seconds
*/

setInterval(
    loadStatus,
    30000
);

setInterval(
    function() {
        loadUptime(byId("latencyRange").value);
    },
    30000
);


/* Incidents + alert toasts: every 30 seconds */
setInterval(loadIncidents, 30000);


/*
    Latency graph:
    Refresh every 60 seconds
*/

setInterval(
    function() {

        const range =
            byId("latencyRange").value;

        const res =
            byId("latencyResolution") && byId("latencyResolution").value;

        loadLatency(range, res);

    },
    60000
);


/*
    Speedtest graph:
    Refresh every 60 seconds

    This does NOT run a new Speedtest.
    It only checks the database for
    newly recorded results.
*/

setInterval(
    function() {

        const range =
            byId("speedtestRange").value;

        const res =
            byId("speedtestResolution") && byId("speedtestResolution").value;

        loadSpeedtest(range, res);

    },
    60000
);


/* ============================================================
   ACTIVE SIDEBAR WHILE SCROLLING
============================================================ */

const sections = document.querySelectorAll(
    ".page-section"
);

const navItems = document.querySelectorAll(
    ".nav-item"
);


window.addEventListener(
    "scroll",
    function() {

        let current = "";

        sections.forEach(
            function(section) {

                const sectionTop =
                    section.offsetTop - 150;

                if (
                    window.scrollY >= sectionTop
                ) {

                    current =
                        section.getAttribute("id");

                }

            }
        );


        navItems.forEach(
            function(item) {

                item.classList.remove("active");

                if (
                    item.getAttribute("href")
                    ===
                    "#" + current
                ) {

                    item.classList.add("active");

                }

            }
        );

    }
);
