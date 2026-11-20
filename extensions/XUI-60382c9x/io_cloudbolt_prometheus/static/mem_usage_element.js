import { GraphElement, hasPoints } from "./graph_element.js";
import { SERIES, byteUnit, maxValue, promOptions, toPoints } from "./chart_theme.js";

export class MemUsageElement extends GraphElement {
    get metric() {
        return "memory";
    }

    build(payload) {
        if (!hasPoints(payload.used, payload.cached, payload.free)) {
            return null;
        }
        const { divisor, unit } = byteUnit(
            maxValue(payload.used, payload.cached, payload.free, payload.buffers)
        );
        const series = [
            { name: "Used", color: SERIES.blue, data: toPoints(payload.used, 1 / divisor) },
            { name: "Cached", color: SERIES.aqua, data: toPoints(payload.cached, 1 / divisor) },
        ];
        // Some sources fold buffers into cached and report none.
        if (maxValue(payload.buffers) > 0) {
            series.push({ name: "Buffers", color: SERIES.violet, data: toPoints(payload.buffers, 1 / divisor) });
        }
        series.push({ name: "Free", color: SERIES.neutral, data: toPoints(payload.free, 1 / divisor) });
        return promOptions({ unit, series });
    }
}
