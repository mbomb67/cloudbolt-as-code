import { GraphElement, hasPoints } from "./graph_element.js";
import { SERIES, byteUnit, maxValue, promOptions, toPoints } from "./chart_theme.js";

export class NetIOGraphElement extends GraphElement {
    get metric() {
        return "net_io";
    }

    build(payload) {
        if (!hasPoints(payload.rx, payload.tx)) {
            return null;
        }
        const { divisor, unit } = byteUnit(maxValue(payload.rx, payload.tx), true);
        return promOptions({
            unit,
            series: [
                { name: "Received", color: SERIES.blue, data: toPoints(payload.rx, 1 / divisor) },
                { name: "Sent", color: SERIES.aqua, data: toPoints(payload.tx, 1 / divisor) },
            ],
        });
    }
}
