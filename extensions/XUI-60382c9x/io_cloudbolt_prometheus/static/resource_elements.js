/**
 * The resource Monitoring tab: tier selector, tiles, table, charts and alerts over the servers a
 * resource deployed. Every element reads its data through one per-tab cache, so the summary and
 * the alerts are fetched once however many cards use them; Refresh and a new tier empty it.
 */
import { GraphElement } from "./graph_element.js";
import { ServerAlertsElement } from "./alerts_element.js";
import {
    BLUE_STEPS,
    CATEGORICAL,
    SERIES,
    byteUnit,
    promOptions,
    tint,
} from "./chart_theme.js";

const API = "/xui/io_cloudbolt_prometheus/api/resources";
const DASH = "—";
const HOT_ABOVE = 80;
const TOP_SERVERS = 5;
const MAX_TIERS = 6;
const KEEP_TIERS = 5;
const NO_FILL = "rgba(0, 0, 0, 0)";
const TIER_COLORS = [...CATEGORICAL, BLUE_STEPS[2]];

// --- dom helpers ----------------------------------------------------------------------------

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
        node.className = className;
    }
    if (text !== undefined && text !== null && text !== "") {
        node.textContent = text;
    }
    return node;
}

function icon(name) {
    const node = el("i", `bi ${name}`);
    node.setAttribute("aria-hidden", "true");
    return node;
}

function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function link(text, url) {
    const node = el("a", "", text);
    if (url) {
        node.href = url;
    }
    return node;
}

function gib(bytes) {
    return `${(bytes / 2 ** 30).toFixed(1)} GiB`;
}

function pct(value) {
    return value === null || value === undefined ? DASH : `${Math.round(value)} %`;
}

// --- tab state: selected tier and the shared cache ------------------------------------------

/**
 * The selected tier is kept in sessionStorage, keyed by resource. It is not written to the URL:
 * CloudBolt's tabs.js reads the whole hash as the id of the tab pane to show.
 */
function storageKey(dash) {
    return `io_prom_tier:${dash.dataset.resourceId}`;
}

function readTier(dash) {
    try {
        return window.sessionStorage.getItem(storageKey(dash)) || "";
    } catch (error) {
        return "";
    }
}

function writeTier(dash, tier) {
    try {
        if (tier) {
            window.sessionStorage.setItem(storageKey(dash), tier);
        } else {
            window.sessionStorage.removeItem(storageKey(dash));
        }
    } catch (error) {
        // Storage can be blocked; the selection then lasts until the page reloads.
    }
}

/** The selected tier name, or "" for all tiers. The first call reads it from sessionStorage. */
export function currentTier(dash) {
    if (dash.dataset.tierReady !== "1") {
        dash.dataset.tier = readTier(dash);
        dash.dataset.tierReady = "1";
    }
    return dash.dataset.tier || "";
}

export function clearCache(dash) {
    dash.promCache = new Map();
}

/** Select a tier ("" for all) and reload every card. */
export function selectTier(dash, tier) {
    currentTier(dash);
    dash.dataset.tier = tier;
    writeTier(dash, tier);
    clearCache(dash);
    dash.dispatchEvent(new CustomEvent("prom-refresh"));
}

function endpoint(dash, metric, withTier) {
    const tier = withTier ? currentTier(dash) : "";
    return `${API}/${dash.dataset.resourceId}/${metric}/${tier ? `?tier=${encodeURIComponent(tier)}` : ""}`;
}

/** The answer of an endpoint, fetched once per tab until the cache is emptied. */
export function sharedFetch(dash, metric, withTier = true) {
    if (!dash.promCache) {
        clearCache(dash);
    }
    const url = endpoint(dash, metric, withTier);
    if (!dash.promCache.has(url)) {
        const request = fetch(url, { cache: "no-store" })
            .then((response) => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response.json();
            })
            .then((payload) => payload || {});
        // A failed request is not kept, so the next Refresh asks again.
        request.catch(() => dash.promCache.delete(url));
        dash.promCache.set(url, request);
    }
    return dash.promCache.get(url);
}

// --- colours --------------------------------------------------------------------------------

/**
 * {tier name: colour}. Up to six tiers each get a palette colour in tier order. With more, the five
 * with the most servers get one and the rest are drawn as "Other tiers" in the neutral grey,
 * the same rule the charts' data follows.
 */
export function tierColors(tiers) {
    const colors = {};
    let shown = tiers.map((tier, index) => index);
    if (tiers.length > MAX_TIERS) {
        shown = shown
            .slice()
            .sort((a, b) => tiers[b].server_count - tiers[a].server_count || a - b)
            .slice(0, KEEP_TIERS)
            .sort((a, b) => a - b);
    }
    tiers.forEach((tier) => {
        colors[tier.name] = SERIES.neutral;
    });
    shown.forEach((tierIndex, position) => {
        colors[tiers[tierIndex].name] = TIER_COLORS[position];
    });
    return colors;
}

/** {server id: colour} for the servers closest to a limit; every other server stays grey. */
export function serverColors(servers) {
    const colors = {};
    servers
        .filter((server) => server.headroom !== null && server.headroom !== undefined)
        .slice(0, TOP_SERVERS)
        .forEach((server, position) => {
            colors[server.id] = CATEGORICAL[position];
        });
    return colors;
}

function dot(color) {
    const node = el("span", "prom-dot");
    node.style.background = color;
    return node;
}

// --- tier selector and header ---------------------------------------------------------------

export class ResourceTiersElement extends GraphElement {
    get metric() {
        return "summary";
    }

    async load() {
        const dash = this.dash;
        const seq = (this.seq = (this.seq || 0) + 1);
        let summary;
        try {
            summary = await sharedFetch(dash, "summary");
        } catch (error) {
            if (seq === this.seq) {
                this.textContent = "";
            }
            return;
        }
        if (seq !== this.seq) {
            return;
        }
        const tiers = summary.tiers || [];
        const selected = currentTier(dash);
        if (selected && !tiers.some((tier) => tier.name === selected)) {
            // The tier is gone (or the link came from another resource): back to all tiers.
            selectTier(dash, "");
            return;
        }
        this.describe(tiers, selected);
        this.textContent = "";
        if (tiers.length < 2) {
            return;
        }
        const nav = el("div", "prom-tiers");
        nav.setAttribute("role", "group");
        nav.setAttribute("aria-label", "Tiers");
        const total = tiers.reduce((sum, tier) => sum + tier.server_count, 0);
        [{ name: "", label: "All tiers" }, ...tiers.map((tier) => ({
            name: tier.name,
            label: `${tier.name} (${tier.server_count})`,
        }))].forEach((entry) => {
            const button = el("button", "prom-tier-pill", entry.label);
            button.type = "button";
            button.setAttribute("aria-pressed", String(entry.name === selected));
            button.addEventListener("click", () => {
                if (entry.name !== selected) {
                    selectTier(dash, entry.name);
                }
            });
            nav.appendChild(button);
        });
        nav.title = `${plural(total, "server")} in ${plural(tiers.length, "tier")}`;
        this.appendChild(nav);
    }

    /** The line under the title and the heading above the charts. */
    describe(tiers, selected) {
        const total = tiers.reduce((sum, tier) => sum + tier.server_count, 0);
        const sub = this.dash.querySelector("[data-prom-subtitle]");
        if (sub) {
            sub.textContent = selected
                ? `${selected} tier · ${plural(tiers.find((t) => t.name === selected).server_count, "server")} · last 24 hours`
                : `${plural(total, "server")} in ${plural(tiers.length, "tier")} · last 24 hours`;
        }
        const heading = this.dash.querySelector("[data-prom-compare-title]");
        const detail = this.dash.querySelector("[data-prom-compare-sub]");
        if (heading) {
            heading.textContent = selected ? "Server comparison" : "Tier comparison";
        }
        if (detail) {
            detail.textContent = selected
                ? "One line per server. The five servers closest to a limit keep the same colour in every chart; the rest are grey."
                : "One line per tier. Where a band is shown, it runs from the tier's lowest to its highest server.";
        }
    }
}

// --- tiles ----------------------------------------------------------------------------------

function tile(label, value, unit, detail) {
    const box = el("div", "prom-tile");
    box.appendChild(el("div", "k", label));
    const v = el("div", "v", value);
    if (unit) {
        v.appendChild(el("span", "prom-of", ` ${unit}`));
    }
    box.appendChild(v);
    if (detail) {
        box.appendChild(el("div", "prom-tile-d", detail));
    }
    return box;
}

function alertCounts(alerts) {
    const firing = alerts.filter((alert) => alert.state === "firing");
    return {
        firing: firing.length,
        critical: firing.filter((alert) => alert.severity === "critical").length,
        warning: firing.filter((alert) => alert.severity === "warning").length,
    };
}

export class ResourceTilesElement extends GraphElement {
    get metric() {
        return "summary";
    }

    async load() {
        const dash = this.dash;
        const seq = (this.seq = (this.seq || 0) + 1);
        let summary;
        try {
            summary = await sharedFetch(dash, "summary");
        } catch (error) {
            if (seq === this.seq) {
                this.showMessage(`Could not load the overview (${error.message}).`, true);
            }
            return;
        }
        const alerts = await sharedFetch(dash, "alerts").catch(() => null);
        if (seq !== this.seq) {
            return;
        }
        const tiles = summary.tiles || {};
        const wrap = el("div", "prom-tiles");
        wrap.appendChild(tile("Servers running", String(tiles.servers_running ?? DASH), "", `of ${tiles.servers_total ?? 0}`));
        wrap.appendChild(tile("Average CPU", pct(tiles.cpu_avg ?? null)));
        wrap.appendChild(tile("Average memory", pct(tiles.mem_avg ?? null)));
        if (alerts && alerts.available) {
            const counts = alertCounts(alerts.alerts || []);
            wrap.appendChild(tile(
                "Firing alerts",
                String(counts.firing),
                "",
                `${plural(counts.warning, "warning")}, ${counts.critical} critical`
            ));
        } else {
            wrap.appendChild(tile("Firing alerts", DASH, "", "Not available"));
        }
        const least = tiles.least_headroom;
        wrap.appendChild(
            least
                ? tile("Least headroom", least.server, "", `${least.metric} ${Math.round(least.value)} %`)
                : tile("Least headroom", DASH)
        );
        this.textContent = "";
        this.appendChild(wrap);
    }
}

// --- table ----------------------------------------------------------------------------------

function cell(...children) {
    const node = el("td");
    children.forEach((child) => child && node.append(child));
    return node;
}

function bar(value) {
    if (value === null || value === undefined) {
        return el("span", "prom-muted", DASH);
    }
    const wrap = el("span", "prom-meter");
    const track = el("span", "prom-bar");
    const fill = el("span", value > HOT_ABOVE ? "prom-bar-fill prom-bar-hot" : "prom-bar-fill");
    fill.style.width = `${Math.min(Math.max(value, 0), 100)}%`;
    track.appendChild(fill);
    wrap.append(track, document.createTextNode(` ${Math.round(value)} %`));
    return wrap;
}

function worst(value, name, url) {
    if (value === null || value === undefined || !name) {
        return null;
    }
    const line = el("div", "prom-sub-line", `max ${Math.round(value)} % on `);
    line.appendChild(link(name, url));
    return line;
}

function diskText(used, total) {
    return used === null || used === undefined || !total ? DASH : `${gib(used)} of ${gib(total)}`;
}

function networkText(rx, tx) {
    if (rx === null || rx === undefined || tx === null || tx === undefined) {
        return DASH;
    }
    const { divisor, unit } = byteUnit(Math.max(rx, tx), true);
    return `${(rx / divisor).toFixed(1)} / ${(tx / divisor).toFixed(1)} ${unit}`;
}

function alertCell(list, available) {
    if (!available) {
        return cell(el("span", "prom-muted", DASH));
    }
    if (!list.length) {
        return cell(el("span", "prom-muted", "None"));
    }
    const severities = list.map((alert) => alert.severity);
    const style = severities.includes("critical") ? "prom-pill-bad" : severities.includes("warning") ? "prom-pill-warn" : "prom-pill-info";
    return cell(el("span", `prom-pill ${style}`, plural(list.length, "alert")));
}

function headRow(labels) {
    const row = el("tr");
    labels.forEach((label) => {
        const th = el("th", "", label);
        th.scope = "col";
        row.appendChild(th);
    });
    return row;
}

export class ResourceTableElement extends GraphElement {
    get metric() {
        return "summary";
    }

    async load() {
        const dash = this.dash;
        const seq = (this.seq = (this.seq || 0) + 1);
        let summary;
        try {
            summary = await sharedFetch(dash, "summary");
        } catch (error) {
            if (seq === this.seq) {
                this.setTitle(currentTier(dash) ? "Servers" : "Tiers", "");
                this.showMessage(`Could not load this table (${error.message}).`, true);
            }
            return;
        }
        const alerts = await sharedFetch(dash, "alerts").catch(() => null);
        if (seq !== this.seq) {
            return;
        }
        const available = !!(alerts && alerts.available);
        const list = (alerts && alerts.alerts) || [];
        const selected = currentTier(dash);
        const tiers = summary.tiers || [];
        // A tier is selected, or the resource has only one: one row per server.
        const serverMode = !!selected || tiers.length < 2;
        const rows = serverMode ? summary.servers || [] : tiers;
        if (!rows.length) {
            this.setTitle(serverMode ? "Servers" : "Tiers", "");
            this.showMessage(selected ? "No servers in this tier." : "This resource has no servers.", false);
            return;
        }
        const table = el("table", "prom-table");
        const body = el("tbody");
        if (serverMode) {
            this.setTitle("Servers", "One row per server, highest use first. Values are the last 5 minutes.");
            table.appendChild(el("thead")).appendChild(
                headRow(["Server", "State", "CPU", "Memory", "Disk", "Network in / out", "Alerts"])
            );
            const colors = serverColors(summary.servers || []);
            rows.forEach((server) => body.appendChild(this.serverRow(
                server, colors[server.id] || SERIES.neutral, list.filter((a) => a.server_id === server.id), available
            )));
        } else {
            this.setTitle("Tiers", "One row per tier. Bars show the tier average; the worst server is named. Select a tier to see its servers.");
            table.appendChild(el("thead")).appendChild(
                headRow(["Tier", "Servers", "CPU", "Memory", "Disk", "Network in / out", "Alerts"])
            );
            const colors = tierColors(tiers);
            rows.forEach((tier) => body.appendChild(this.tierRow(
                tier, colors[tier.name], list.filter((a) => a.tier === tier.name), available
            )));
        }
        table.appendChild(body);
        const scroll = el("div", "prom-table-scroll");
        scroll.appendChild(table);
        this.clear();
        this.appendChild(scroll);
        if (!rows.some((row) => (serverMode ? row.monitored : row.cpu_avg !== null || row.mem_avg !== null))) {
            this.appendChild(el("div", "prom-empty", serverMode
                ? "No monitored servers in this tier. Their numbers are shown as dashes."
                : "No numbers yet: the monitoring source returned nothing for these servers."));
        }
    }

    setTitle(title, text) {
        const card = this.closest(".prom-card");
        const t = card && card.querySelector("[data-prom-title]");
        if (t) {
            t.textContent = title;
        }
        this.setSubtitle(text);
    }

    tierRow(tier, color, alerts, available) {
        const row = el("tr");
        const name = el("button", "prom-tier-link", tier.name);
        name.type = "button";
        name.title = `Show the servers of ${tier.name}`;
        name.addEventListener("click", () => selectTier(this.dash, tier.name));
        const label = el("div", "prom-name");
        label.append(dot(color), name);
        row.appendChild(cell(label));
        const running = tier.running_count === tier.server_count
            ? el("span", "", `${tier.running_count} running`)
            : el("span", "prom-pill prom-pill-warn", `${tier.running_count} of ${tier.server_count} running`);
        row.appendChild(cell(running));
        row.appendChild(cell(bar(tier.cpu_avg), worst(tier.cpu_max, tier.cpu_max_server, tier.cpu_max_server_url)));
        row.appendChild(cell(bar(tier.mem_avg), worst(tier.mem_max, tier.mem_max_server, tier.mem_max_server_url)));
        row.appendChild(cell(document.createTextNode(diskText(tier.disk_used, tier.disk_total))));
        row.appendChild(cell(document.createTextNode(networkText(tier.net_rx, tier.net_tx))));
        row.appendChild(alertCell(alerts, available));
        return row;
    }

    serverRow(server, color, alerts, available) {
        const row = el("tr");
        const label = el("div", "prom-name");
        label.append(dot(color), link(server.hostname, server.url));
        row.appendChild(cell(label));
        const ok = server.state === "Running";
        const stopped = server.state === "Stopped";
        row.appendChild(cell(el("span", `prom-pill ${ok ? "prom-pill-ok" : stopped ? "" : "prom-pill-warn"}`.trim(), server.state)));
        row.appendChild(cell(bar(server.cpu)));
        row.appendChild(cell(bar(server.mem)));
        row.appendChild(cell(document.createTextNode(diskText(server.disk_used, server.disk_total))));
        row.appendChild(cell(document.createTextNode(networkText(server.net_rx, server.net_tx))));
        row.appendChild(alertCell(alerts, available && server.monitored));
        if (!server.monitored) {
            row.title = "This server has no monitoring source.";
        }
        return row;
    }
}

// --- charts ---------------------------------------------------------------------------------

const GRAPH_TEXT = {
    cpu: {
        tiers: "Tier average, in percent. Band: lowest to highest server.",
        servers: "Share of each server's CPUs in use, in percent.",
    },
    memory: {
        tiers: "Tier average, in percent. Band: lowest to highest server.",
        servers: "Share of each server's memory in use, in percent.",
    },
    net_io: {
        tiers: "Tier total, received plus sent, per second.",
        servers: "Received plus sent by each server, per second.",
    },
    disk_io: {
        tiers: "Tier total, read plus written, per second.",
        servers: "Read plus written by each server, per second.",
    },
};

/** A scaled copy of [ms, value] points. */
function scaled(points, factor) {
    return points.map((point) => [point[0], point[1] * factor]);
}

function maxOf(series) {
    let max = 0;
    series.forEach((s) => s.data.forEach((point) => {
        max = Math.max(max, point[1]);
        (s.band || []).forEach((b) => {
            max = Math.max(max, b[2]);
        });
    }));
    return max;
}

/**
 * One chart card. The lowest-to-highest band is drawn as two thin, faint lines in the tier's colour
 * that have no legend entry: current CloudBolt draws these options with ECharts through a layer that
 * does not read Highcharts' arearange, and older CloudBolt draws plain line series the same way.
 */
export class ResourceGraphElement extends GraphElement {
    get metric() {
        return this.getAttribute("metric");
    }

    async fetchPayload() {
        return sharedFetch(this.dash, this.metric);
    }

    async load() {
        const seq = (this.seq = (this.seq || 0) + 1);
        let payload;
        let summary;
        try {
            [payload, summary] = await Promise.all([
                this.fetchPayload(),
                sharedFetch(this.dash, "summary").catch(() => null),
            ]);
        } catch (error) {
            if (seq === this.seq) {
                this.showMessage(`Could not load this chart (${error.message}).`, true);
            }
            return;
        }
        if (seq !== this.seq) {
            return;
        }
        if (!payload || !payload.mode) {
            this.showMessage("Monitoring data is not available for this resource.", true);
            return;
        }
        const monitored = summary ? (summary.servers || []).some((server) => server.monitored) : true;
        const options = monitored ? this.build(payload || {}, summary) : null;
        if (!monitored) {
            this.showMessage(
                currentTier(this.dash) ? "No monitored servers in this tier." : "No monitored servers in this resource.",
                false
            );
        } else if (!options) {
            this.showMessage("The monitoring source returned no data for these servers in the last 24 hours.", false);
        } else {
            this.draw(options);
        }
    }

    build(payload, summary) {
        const series = payload.series || [];
        if (!series.some((s) => s.data && s.data.length)) {
            return null;
        }
        const text = GRAPH_TEXT[this.metric] || {};
        this.setSubtitle(text[payload.mode] || "");
        const isBytes = payload.unit === "bytes/s";
        const unit = isBytes ? byteUnit(maxOf(series), true) : { divisor: 1, unit: "%" };
        const factor = 1 / unit.divisor;
        const built = payload.mode === "servers"
            ? this.serverSeries(series, summary, factor)
            : this.tierSeries(series, summary, factor);
        const options = promOptions({ unit: unit.unit, series: built });
        // Plain lines: with up to ten overlapping series an area under each one is unreadable.
        options.chart.type = "line";
        if (!isBytes) {
            options.yAxis.max = 100;
        }
        this.hideFromTooltip(options, built.filter((s) => s.skipTooltip).map((s) => s.name));
        return options;
    }

    /** Band lines and grey server lines stay out of the shared tooltip even if the engine ignores enableMouseTracking. */
    hideFromTooltip(options, names) {
        const skip = new Set(names);
        const original = options.tooltip.formatter;
        options.tooltip.formatter = function () {
            if (!this.points) {
                return original.call(this);
            }
            const context = Object.create(this);
            context.points = this.points.filter((point) => !skip.has(point.series.name));
            return context.points.length ? original.call(context) : false;
        };
    }

    tierSeries(series, summary, factor) {
        const colors = tierColors((summary && summary.tiers) || []);
        const out = [];
        series.forEach((s) => {
            const color = colors[s.name] || SERIES.neutral;
            out.push({
                name: `${s.name} (${s.server_count})`,
                color,
                data: scaled(s.data, factor),
            });
            if (s.band) {
                // Highcharts would call these an arearange; two faint lines say the same on both engines.
                const faint = tint(color, 0.45);
                [[1, "lowest"], [2, "highest"]].forEach(([column, word]) => out.push({
                    name: `${s.name}, ${word} server`,
                    color: faint,
                    lineWidth: 1,
                    fillColor: NO_FILL,
                    showInLegend: false,
                    enableMouseTracking: false,
                    skipTooltip: true,
                    data: s.band.map((point) => [point[0], point[column] * factor]),
                }));
            }
        });
        return out;
    }

    serverSeries(series, summary, factor) {
        const colors = serverColors((summary && summary.servers) || []);
        const out = [];
        let others = 0;
        series.forEach((s) => {
            const color = colors[s.id];
            if (color) {
                out.push({ name: s.hostname, color, data: scaled(s.data, factor) });
                return;
            }
            others += 1;
            out.push({
                name: s.hostname,
                color: SERIES.neutral,
                lineWidth: 1,
                fillColor: NO_FILL,
                showInLegend: false,
                enableMouseTracking: false,
                skipTooltip: true,
                data: scaled(s.data, factor),
            });
        });
        if (others) {
            // One legend entry for all the grey lines.
            out.push({
                name: plural(others, "other server"),
                color: SERIES.neutral,
                lineWidth: 1,
                fillColor: NO_FILL,
                enableMouseTracking: false,
                skipTooltip: true,
                data: [],
            });
        }
        return out;
    }
}

// --- alerts ---------------------------------------------------------------------------------

function alertHead() {
    const head = el("div", "prom-alert-row prom-alert-head");
    head.append(
        el("div", "prom-alert-sev", "Severity"),
        el("div", "prom-alert-text", "Alert"),
        el("div", "prom-alert-server", "Server"),
        el("div", "prom-alert-tier", "Tier"),
        el("div", "prom-alert-meta", "Since")
    );
    return head;
}

/** The existing alerts card, over the resource's servers, with Server and Tier columns. */
export class ResourceAlertsElement extends ServerAlertsElement {
    get scopeText() {
        return currentTier(this.dash) ? "in this tier" : "for the servers of this resource";
    }

    unavailableText(payload) {
        return payload.monitored_count === 0
            ? "No monitored servers in this selection."
            : "Alerts could not be read from the monitoring source.";
    }

    async fetchPayload() {
        return sharedFetch(this.dash, "alerts");
    }

    renderCard(header, body) {
        super.renderCard(header, body);
        if (body && body.classList.contains("prom-alert-rows")) {
            // Head and rows scroll together inside the card at narrow widths.
            const scroll = el("div", "prom-alert-scroll");
            const table = el("div", "prom-alert-table");
            this.card.insertBefore(scroll, body);
            table.append(alertHead(), body);
            scroll.appendChild(table);
        }
    }

    row(alert) {
        const row = super.row(alert);
        const server = el("div", "prom-alert-server");
        server.appendChild(link(alert.hostname || DASH, alert.url));
        row.insertBefore(server, row.querySelector(".prom-alert-meta"));
        row.insertBefore(el("div", "prom-alert-tier", alert.tier || DASH), row.querySelector(".prom-alert-meta"));
        return row;
    }
}
