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

export class DiskUsageGraphElement extends GraphElement {
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
        var seriesData = [];
        payload.forEach((filesystem, i) => {
            seriesData[i] = {
                name: filesystem.metric.mountpoint,
                data: filesystem.values.map((a) => [
                    a[0] * 1000,
                    parseFloat(a[1]),
                ]),
            };
        });

        this.options = {
            title: { text: undefined },
            credits: promCredits("Disk usage"),
            legend: promLegendCompact,
            chart: promChartCommon({
                type: "area",
                height: PROM_CHART_HEIGHT,
            }),
            tooltip: {
                shared: true,
                crosshairs: true,
            },
            plotOptions: {
                area: {
                    marker: { enabled: false },
                },
            },
            xAxis: [
                {
                    ...promXAxisDatetime,
                    labels: {
                        ...promXAxisDatetime.labels,
                        step: 1,
                    },
                    showEmpty: false,
                },
            ],
            yAxis: [promYAxisCompact],
            series: seriesData,
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
            `/xui/io_cloudbolt_prometheus/api/servers/${server_id}/disk_usage/`
        );
        this.render_component(await response.json());
    }
}
