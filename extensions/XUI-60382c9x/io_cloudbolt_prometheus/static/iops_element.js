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

export class IopsGraphElement extends GraphElement {
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
        payload["iops"] = payload["iops"].map((a) => [
            a[0] * 1000,
            parseInt(a[1], 10),
        ]);
        payload["io_time"] = payload["io_time"].map((a) => [
            a[0] * 1000,
            parseFloat(a[1]),
        ]);

        const yAxisDual = [
            promYAxisCompact,
            {
                title: { text: null },
                opposite: true,
                maxPadding: 0.02,
                labels: {
                    style: { fontSize: "10px", color: "#64748b" },
                    x: 4,
                },
                gridLineColor: "rgba(148, 163, 184, 0.2)",
            },
        ];

        this.options = {
            title: { text: undefined },
            credits: promCredits("IOPS"),
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
            yAxis: yAxisDual,
            plotOptions: {
                area: {
                    marker: { enabled: false },
                },
            },
            series: [
                {
                    name: "reads",
                    data: payload["reads"],
                    animation: false,
                },
                {
                    name: "writes",
                    data: payload["writes"],
                    animation: false,
                },
                {
                    name: "IOPS",
                    data: payload["iops"],
                    animation: false,
                },
                {
                    name: "IO time",
                    data: payload["io_time"],
                    animation: false,
                    yAxis: 1,
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
            `/xui/io_cloudbolt_prometheus/api/servers/${server_id}/iops/`
        );
        this.render_component(await response.json());
    }
}
