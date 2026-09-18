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

export class NetIOGraphElement extends GraphElement {
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
        payload["rx"] = payload["rx"].map((a) => [
            a[0] * 1000,
            parseFloat(a[1]),
        ]);
        payload["tx"] = payload["tx"].map((a) => [
            a[0] * 1000,
            parseFloat(a[1]),
        ]);

        this.options = {
            title: { text: undefined },
            credits: promCredits("Network I/O"),
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
                    name: "sent",
                    data: payload["tx"],
                    animation: false,
                    stack: 1,
                },
                {
                    name: "received",
                    data: payload["rx"],
                    animation: false,
                    stack: 1,
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
            `/xui/io_cloudbolt_prometheus/api/servers/${server_id}/net_io/`
        );
        this.render_component(await response.json());
    }
}
