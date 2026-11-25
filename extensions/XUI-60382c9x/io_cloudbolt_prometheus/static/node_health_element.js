import { GraphElement } from "./graph_element.js";

const DASH = "—";

function gib(bytes) {
    const value = parseFloat(bytes);
    return Number.isFinite(value) && value >= 0 ? `${(value / 2 ** 30).toFixed(1)} GiB` : DASH;
}

function percent(fraction) {
    const value = parseFloat(fraction);
    return Number.isFinite(value) && value >= 0 ? `${(value * 100).toFixed(2)}%` : DASH;
}

function duration(seconds) {
    const total = parseFloat(seconds);
    if (!Number.isFinite(total) || total < 0) {
        return DASH;
    }
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days) {
        return `${days} d ${hours} h`;
    }
    return hours ? `${hours} h ${minutes} m` : `${minutes} m`;
}

/** Space in use out of the disk given to the server; the total alone when usage is unknown. */
function diskTile(payload) {
    const used = parseFloat(payload.disk_used);
    const total = parseFloat(payload.disk_total);
    if (!Number.isFinite(used) || used < 0 || !Number.isFinite(total) || total <= 0) {
        return ["Disk", gib(payload.disk_total)];
    }
    return ["Disk used", gib(used), `of ${gib(total)}`];
}

/** The stat tiles at the top of the monitoring tab. */
export class NodeHealthElement extends GraphElement {
    get metric() {
        return "health";
    }

    async load() {
        let payload;
        try {
            payload = (await this.fetchPayload()) || {};
        } catch (error) {
            this.showMessage(`Could not load the server overview (${error.message}).`, true);
            return;
        }
        // OpenShift Virtualization VMs report CPU utilization and have no exporter to measure.
        const fromCluster = payload.source === "kubevirt";
        const cores = parseInt(payload.core_count, 10);
        const tiles = [
            ["Cores", Number.isFinite(cores) && cores >= 0 ? String(cores) : DASH],
            ["Memory", gib(payload.mem_total)],
            diskTile(payload),
            [fromCluster ? "CPU" : "Load", percent(payload.load)],
            ["Uptime", duration(payload.uptime)],
        ];
        if (!fromCluster) {
            tiles.push(["Exporter CPU", percent(payload.scraper_cpu_use)]);
        }

        this.textContent = "";
        const grid = document.createElement("div");
        grid.className = "prom-stats";
        tiles.forEach(([label, value, detail]) => {
            const tile = document.createElement("div");
            tile.className = "prom-stat";
            const k = document.createElement("div");
            k.className = "k";
            k.textContent = label;
            const v = document.createElement("div");
            v.className = "v";
            v.textContent = value;
            if (detail) {
                const of = document.createElement("span");
                of.className = "prom-of";
                of.textContent = detail;
                v.appendChild(of);
            }
            tile.append(k, v);
            grid.appendChild(tile);
        });
        this.appendChild(grid);
    }
}
