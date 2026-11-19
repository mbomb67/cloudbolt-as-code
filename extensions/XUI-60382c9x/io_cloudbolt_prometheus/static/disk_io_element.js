import { GraphElement, hasPoints } from "./graph_element.js";
import { SERIES, byteUnit, maxValue, promOptions, toPoints } from "./chart_theme.js";

export class DiskIOGraphElement extends GraphElement {
    get metric() {
        return "disk_io";
    }

    build(payload) {
        if (!hasPoints(payload.reads, payload.writes, payload.total)) {
            return null;
        }
        const { divisor, unit } = byteUnit(maxValue(payload.reads, payload.writes, payload.total), true);
        return promOptions({
            unit,
            series: [
                { name: "Read", color: SERIES.blue, data: toPoints(payload.reads, 1 / divisor) },
                { name: "Written", color: SERIES.aqua, data: toPoints(payload.writes, 1 / divisor) },
                { name: "Total", color: SERIES.violet, data: toPoints(payload.total, 1 / divisor) },
            ],
        });
    }
}
