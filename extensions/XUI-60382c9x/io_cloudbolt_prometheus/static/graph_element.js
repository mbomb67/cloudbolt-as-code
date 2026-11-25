import { isRealHighcharts, localTimeOptions } from "./chart_theme.js";

/**
 * Base class for a monitoring card's chart. A subclass names its metric and
 * builds chart options from the payload; this class fetches the payload,
 * reloads on the tab's Refresh button, and shows the empty and error states.
 */
export class GraphElement extends HTMLElement {
    /** The last path segment of the metrics endpoint, e.g. "cpu_load". */
    get metric() {
        return "";
    }

    connectedCallback() {
        this.serverId = this.getAttribute("server-id");
        this.dash = this.closest(".io-prom-dash");
        this.onRefresh = () => this.load();
        if (this.dash) {
            this.dash.addEventListener("prom-refresh", this.onRefresh);
        }
        // Real Highcharts measures its container once, when it creates the chart, and uses 600 px when
        // the container has no width (the tab's pane is still hidden). So it waits for a width and
        // is resized when the window changes or the pane is shown.
        this.onResize = () => this.reflow();
        window.addEventListener("resize", this.onResize);
        document.addEventListener("shown.bs.tab", this.onResize);
        if (typeof ResizeObserver === "function") {
            this.sizeWatcher = new ResizeObserver(this.onResize);
            this.sizeWatcher.observe(this);
        }
        this.load();
    }

    /** Create the chart once the card has a width; resize an existing one (real Highcharts only). */
    reflow() {
        if (!isRealHighcharts()) {
            return;
        }
        if (this.pendingOptions) {
            if (this.offsetWidth > 0) {
                const options = this.pendingOptions;
                this.pendingOptions = null;
                this.create(options);
            }
            return;
        }
        const chart = this.chartDiv && $(this.chartDiv).highcharts && $(this.chartDiv).highcharts();
        if (chart && typeof chart.reflow === "function") {
            chart.reflow();
        }
    }

    disconnectedCallback() {
        window.removeEventListener("resize", this.onResize);
        document.removeEventListener("shown.bs.tab", this.onResize);
        if (this.sizeWatcher) {
            this.sizeWatcher.disconnect();
        }
        if (this.dash) {
            this.dash.removeEventListener("prom-refresh", this.onRefresh);
        }
    }

    /** Chart options for the payload, or null when it holds no data. */
    build(payload) {
        return null;
    }

    /** Set the line under the card's title, e.g. the unit the chart is drawn in. */
    setSubtitle(text) {
        const card = this.closest(".prom-card");
        const sub = card && card.querySelector("[data-prom-sub]");
        if (sub) {
            sub.textContent = text;
        }
    }

    async fetchPayload() {
        const response = await fetch(
            `/xui/io_cloudbolt_prometheus/api/servers/${this.serverId}/${this.metric}/`,
            { cache: "no-store" }
        );
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
    }

    async load() {
        let payload;
        try {
            payload = await this.fetchPayload();
        } catch (error) {
            this.showMessage(`Could not load this chart (${error.message}).`, true);
            return;
        }
        const options = this.build(payload || {});
        if (!options) {
            this.showMessage("No data in the last 24 hours.", false);
            return;
        }
        this.draw(options);
    }

    clear() {
        const chart = this.chartDiv && $(this.chartDiv).highcharts && $(this.chartDiv).highcharts();
        if (chart && chart.destroy) {
            chart.destroy();
        }
        this.textContent = "";
        this.chartDiv = null;
        this.pendingOptions = null;
    }

    draw(options) {
        this.clear();
        this.pendingOptions = null;
        if (isRealHighcharts() && !this.offsetWidth) {
            this.pendingOptions = options;
            return;
        }
        this.create(options);
    }

    create(options) {
        this.chartDiv = document.createElement("div");
        this.appendChild(this.chartDiv);
        $(this.chartDiv).highcharts(localTimeOptions(options));
    }

    showMessage(text, isError) {
        this.clear();
        const box = document.createElement("div");
        box.className = isError ? "prom-alert" : "prom-empty";
        if (isError) {
            const icon = document.createElement("i");
            icon.className = "bi bi-exclamation-triangle-fill";
            icon.setAttribute("aria-hidden", "true");
            box.appendChild(icon);
        }
        box.appendChild(document.createTextNode(text));
        this.appendChild(box);
    }
}

/** True when any of the lists holds a point. */
export function hasPoints(...lists) {
    return lists.some((values) => values && values.length > 0);
}
