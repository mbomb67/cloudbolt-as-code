import { GraphElement, hasPoints } from "./graph_element.js";
import { BLUE_STEPS, promOptions, toPoints } from "./chart_theme.js";

export class CpuLoadGraphElement extends GraphElement {
    get metric() {
        return "cpu_load";
    }

    build(payload) {
        if (!hasPoints(payload.load1, payload.load5, payload.load15)) {
            return null;
        }
        // A source that reports CPU utilization instead of load average names its own series.
        const names = payload.names || {};
        this.setSubtitle(
            payload.names
                ? "Utilization, as a share of all CPUs"
                : "Load average, as a share of the CPU count"
        );
        return promOptions({
            unit: "%",
            series: [
                { name: names.load1 || "1 min load", color: BLUE_STEPS[0], data: toPoints(payload.load1, 100) },
                { name: names.load5 || "5 min load", color: BLUE_STEPS[1], data: toPoints(payload.load5, 100) },
                { name: names.load15 || "15 min load", color: BLUE_STEPS[2], data: toPoints(payload.load15, 100) },
            ],
        });
    }
}
