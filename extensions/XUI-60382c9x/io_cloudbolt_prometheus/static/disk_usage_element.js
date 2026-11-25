import { GraphElement } from "./graph_element.js";
import { CATEGORICAL, byteUnit, maxValue, promOptions, toPoints } from "./chart_theme.js";

export class DiskUsageGraphElement extends GraphElement {
    get metric() {
        return "disk_usage";
    }

    build(payload) {
        // One series of free bytes per mount point.
        const filesystems = Array.isArray(payload) ? payload.filter((fs) => fs.values.length) : [];
        if (!filesystems.length) {
            return null;
        }
        const { divisor, unit } = byteUnit(maxValue(...filesystems.map((fs) => fs.values)));
        return promOptions({
            unit,
            series: filesystems.map((fs, i) => ({
                name: fs.metric.mountpoint,
                color: CATEGORICAL[i % CATEGORICAL.length],
                data: toPoints(fs.values, 1 / divisor),
            })),
        });
    }
}
