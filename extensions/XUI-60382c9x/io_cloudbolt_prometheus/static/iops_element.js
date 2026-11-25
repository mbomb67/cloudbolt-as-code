import { GraphElement, hasPoints } from "./graph_element.js";
import { SERIES, promOptions, toPoints } from "./chart_theme.js";

export class IopsGraphElement extends GraphElement {
    get metric() {
        return "iops";
    }

    // The payload's io_time is seconds per second, a different unit, so it is not drawn here.
    build(payload) {
        if (!hasPoints(payload.reads, payload.writes, payload.iops)) {
            return null;
        }
        return promOptions({
            unit: "ops/s",
            series: [
                { name: "Reads", color: SERIES.blue, data: toPoints(payload.reads) },
                { name: "Writes", color: SERIES.aqua, data: toPoints(payload.writes) },
                { name: "Total", color: SERIES.violet, data: toPoints(payload.iops) },
            ],
        });
    }
}
