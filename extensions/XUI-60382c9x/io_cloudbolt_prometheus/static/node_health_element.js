import { html, render } from "https://unpkg.com/lit-html";
import { GraphElement } from "./graph_element.js";

/** Parse rgb/rgba from getComputedStyle; return rgba(..., alpha). */
function colorToRgbaAtAlpha(cssColor, alpha) {
    if (!cssColor || cssColor === "transparent") {
        return null;
    }
    const m = cssColor.match(
        /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)/
    );
    if (!m) {
        return null;
    }
    const r = Number(m[1]);
    const g = Number(m[2]);
    const b = Number(m[3]);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function isNonTransparentColor(cssColor) {
    return (
        cssColor &&
        cssColor !== "transparent" &&
        cssColor !== "rgba(0, 0, 0, 0)" &&
        !/^rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\)$/.test(cssColor)
    );
}

/** Prefer a Day-2 action button with a visible fill; else first link button text color. */
function pickCbBtnLinkSample() {
    const buttons = document.querySelectorAll("button.cb-btn-link");
    for (const btn of buttons) {
        const bg = getComputedStyle(btn).backgroundColor;
        if (isNonTransparentColor(bg)) {
            return bg;
        }
    }
    if (buttons.length > 0) {
        const fg = getComputedStyle(buttons[0]).color;
        if (isNonTransparentColor(fg)) {
            return fg;
        }
    }
    return null;
}

export class NodeHealthElement extends GraphElement {
    connectedCallback() {
        super.connectedCallback();
        this.scheduleHealthCardThemeFromCbButton();
    }

    scheduleHealthCardThemeFromCbButton() {
        const run = () => this.applyHealthCardThemeFromCbButton();
        run();
        requestAnimationFrame(run);
        setTimeout(run, 0);
        setTimeout(run, 50);
        setTimeout(run, 250);
    }

    applyHealthCardThemeFromCbButton() {
        const card = this.closest(".prom-health-card");
        if (!card) {
            return;
        }
        const src = pickCbBtnLinkSample();
        const rgba = src ? colorToRgbaAtAlpha(src, 0.5) : null;
        if (rgba) {
            card.style.setProperty("--prom-health-bg", rgba);
        }
    }

    render_component(payload) {
        let uptime = (payload["uptime"] / (60 * 60 * 24)).toFixed(2);
        const coresRaw = parseInt(payload["core_count"], 10);
        const cores =
            Number.isFinite(coresRaw) && coresRaw >= 0 ? String(coresRaw) : "—";
        let diskTotal = (parseInt(payload["disk_total"]) / 2 ** 30).toFixed(1);
        let memTotal = (parseInt(payload["mem_total"]) / 2 ** 30).toFixed(1);
        const loadRaw = parseFloat(payload["load"]);
        const load = Number.isFinite(loadRaw)
            ? (loadRaw * 100).toFixed(2)
            : "—";
        let scraperCpuUse = (
            parseFloat(payload["scraper_cpu_use"]) * 100
        ).toFixed(2);

        const markup = html`
            <div class="prom-health-metrics">
                <div class="prom-health-metric">
                    <span class="prom-health-label">Cores</span>
                    <span class="prom-health-value">${cores}</span>
                </div>
                <div class="prom-health-metric">
                    <span class="prom-health-label">Disk total</span>
                    <span class="prom-health-value">${diskTotal} GiB</span>
                </div>
                <div class="prom-health-metric">
                    <span class="prom-health-label">Memory</span>
                    <span class="prom-health-value">${memTotal} GiB</span>
                </div>
                <div class="prom-health-metric">
                    <span class="prom-health-label">Load</span>
                    <span class="prom-health-value">${load}</span>
                </div>
                <div class="prom-health-metric">
                    <span class="prom-health-label">Uptime</span>
                    <span class="prom-health-value">${uptime} d</span>
                </div>
                <div class="prom-health-metric">
                    <span class="prom-health-label">Scraper CPU</span>
                    <span class="prom-health-value">${scraperCpuUse}</span>
                </div>
            </div>
        `;
        render(markup, this);
    }

    async load(server_id) {
        const response = await fetch(
            `/xui/io_cloudbolt_prometheus/api/servers/${server_id}/health/`
        );
        this.render_component(await response.json());
    }
}
