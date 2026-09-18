import { GraphElement } from "./graph_element.js";
import { html, render } from "https://unpkg.com/lit-html";
import {
    PROM_CHART_HEIGHT,
    promCredits,
    promChartCommon,
    promLegendCompact,
    promXAxisDatetime,
    promYAxisCompact,
} from "./chart_theme.js";

export class DiskIOGraphElement extends GraphElement {
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
        payload["reads"] = payload["reads"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10),
        ]);
        payload["writes"] = payload["writes"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10),
        ]);
        payload["total"] = payload["total"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10),
        ]);

        this.options = {
            title: { text: undefined },
            credits: promCredits("Disk I/O"),
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
            xAxis: [promXAxisDatetime],
            yAxis: [promYAxisCompact],
            plotOptions: {
                area: {
                    marker: { enabled: false },
                },
            },
            series: [
                {
                    name: "read",
                    data: payload["reads"],
                    animation: false,
                    lineWidth: 1,
                },
                {
                    name: "written",
                    data: payload["writes"],
                    animation: false,
                    lineWidth: 1,
                },
                {
                    name: "total",
                    data: payload["total"],
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
            `/xui/io_cloudbolt_prometheus/api/servers/${server_id}/disk_io/`
        );
        this.render_component(await response.json());
    }
}
