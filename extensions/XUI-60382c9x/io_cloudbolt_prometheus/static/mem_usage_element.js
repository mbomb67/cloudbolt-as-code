import { html, render } from "https://unpkg.com/lit-html";
import { GraphElement } from "./graph_element.js";
import {
    PROM_CHART_HEIGHT,
    promCredits,
    promChartCommon,
    promLegendCompact,
    promXAxisDatetime,
    promYAxisCompact,
} from "./chart_theme.js";

export class MemUsageElement extends GraphElement {
    connectedCallback() {
        const server_id = this.getAttribute("server-id");
        this.chartDiv = document.createElement("div");
        this.controlsDiv = document.createElement("div");
        this.controlsDiv.className = "prom-refresh-toolbar";

        render(
            html`
                <button
                    type="button"
                    class="prom-refresh-btn"
                    @click="${() => this.load(server_id)}"
                >
                    Refresh
                </button>
            `,
            this.controlsDiv
        );

        this.appendChild(this.controlsDiv);
        this.appendChild(this.chartDiv);

        this.load(server_id);
    }

    render_component(payload) {
        payload["used"] = payload["used"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10) / 2 ** 30,
        ]);
        payload["buffers"] = payload["buffers"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10) / 2 ** 30,
        ]);
        payload["cached"] = payload["cached"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10) / 2 ** 30,
        ]);
        payload["free"] = payload["free"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10) / 2 ** 30,
        ]);

        this.options = {
            title: { text: undefined },
            credits: promCredits("Memory"),
            legend: promLegendCompact,
            chart: promChartCommon({
                type: "area",
                zoomType: "x",
                height: PROM_CHART_HEIGHT,
            }),
            xAxis: [promXAxisDatetime],
            yAxis: [promYAxisCompact],
            plotOptions: {
                area: {
                    marker: { enabled: false },
                },
            },
            tooltip: {
                shared: true,
                crosshairs: true,
            },
            series: [
                {
                    name: "Free",
                    data: payload["free"],
                    animation: false,
                    stacking: "normal",
                },
                {
                    name: "Cached",
                    data: payload["cached"],
                    animation: false,
                    stacking: "normal",
                },
                {
                    name: "Used",
                    data: payload["used"],
                    animation: false,
                    stacking: "normal",
                },
                {
                    name: "Buffers",
                    data: payload["buffers"],
                    animation: false,
                    stacking: "normal",
                },
            ],
        };

        const $el = $(this.chartDiv);
        const existing = $el.highcharts && $el.highcharts();
        if (existing) {
            existing.destroy();
        }
        $el.highcharts(this.options);
    }

    async load(server_id) {
        const response = await fetch(
            `/xui/io_cloudbolt_prometheus/api/servers/${server_id}/memory/`
        );
        this.render_component(await response.json());
    }
}
