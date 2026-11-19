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
        this.load();
    }

    disconnectedCallback() {
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
    }

    draw(options) {
        this.clear();
        this.chartDiv = document.createElement("div");
        this.appendChild(this.chartDiv);
        $(this.chartDiv).highcharts(options);
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
