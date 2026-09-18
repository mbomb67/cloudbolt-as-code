import { html, render } from "https://unpkg.com/lit-html";
import { GraphElement } from "./graph_element.js";
import {
    PROM_CHART_HEIGHT,
    promCredits,
    promChartCommon,
    promLegendCompact,
    promXAxisDatetime,
} from "./chart_theme.js";

export class CpuLoadGraphElement extends GraphElement {
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
        payload["load1"] = payload["load1"].map((a) => [
            a[0] * 1000,
            parseFloat(a[1]) * 100,
        ]);
        payload["load5"] = payload["load5"].map((a) => [
            a[0] * 1000,
            parseFloat(a[1]) * 100,
        ]);
        payload["load15"] = payload["load15"].map((a) => [
            a[0] * 1000,
            parseFloat(a[1]) * 100,
        ]);

        this.options = {
            title: { text: undefined },
            credits: promCredits("CPU load"),
            legend: promLegendCompact,
            chart: promChartCommon({
                type: "area",
                zoomType: "x",
                height: PROM_CHART_HEIGHT,
            }),
            tooltip: {
                shared: true,
                crosshairs: true,
            },
            xAxis: promXAxisDatetime,
            yAxis: {
                title: { text: undefined },
                maxPadding: 0.02,
                labels: {
                    style: { fontSize: "10px", color: "#64748b" },
                    x: -2,
                    formatter: function () {
                        return this.value + "%";
                    },
                },
                gridLineColor: "rgba(148, 163, 184, 0.2)",
            },
            plotOptions: {
                area: {
                    marker: { enabled: false },
                },
            },
            series: [
                {
                    name: "1 min load",
                    data: payload["load1"],
                    animation: false,
                    lineWidth: 1,
                },
                {
                    name: "5 min load",
                    data: payload["load5"],
                    animation: false,
                    lineWidth: 1,
                },
                {
                    name: "15 min load",
                    data: payload["load15"],
                    animation: false,
                    lineWidth: 1,
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
            `/xui/io_cloudbolt_prometheus/api/servers/${server_id}/cpu_load/`
        );
        this.render_component(await response.json());
    }
}
