/**
 * Shared chart styling for the monitoring tab.
 *
 * Current CloudBolt draws these Highcharts-shaped options with ECharts through
 * a compatibility layer that reads a subset of them; older CloudBolt draws
 * them with Highcharts. Only options both understand are used here: a color,
 * line width, area fill and marker switch per series, a format template for
 * axis labels, and a tooltip formatter. Chart titles live in the card's HTML.
 */
export const PROM_CHART_HEIGHT = 230;

const DARK_THEME = document.documentElement.getAttribute("data-bs-theme") === "dark";

/**
 * Series colors, checked for color-blind separation on light and dark
 * surfaces. No red, orange or yellow: those are kept for errors and warnings.
 */
export const SERIES = DARK_THEME
    ? { blue: "#3987e5", aqua: "#199e70", violet: "#9085e9", magenta: "#d55181", green: "#0ca30c" }
    : { blue: "#2a78d6", aqua: "#1baf7a", violet: "#6250d6", magenta: "#e87ba4", green: "#008300" };
SERIES.neutral = "#898781";

/** For series with no fixed meaning (mount points), taken in this order. */
export const CATEGORICAL = [SERIES.blue, SERIES.aqua, SERIES.violet, SERIES.magenta, SERIES.green];

/** One hue in three steps, least to most prominent, for the same measure at three averaging windows. */
export const BLUE_STEPS = DARK_THEME
    ? ["#184f95", "#3987e5", "#9ec5f4"]
    : ["#86b6ef", "#2a78d6", "#104281"];

/** A #rgb or #rrggbb token from /portal.css as [r, g, b], or null. */
function themeRgb(token) {
    let hex = getComputedStyle(document.documentElement)
        .getPropertyValue(token)
        .trim()
        .replace(/^#/, "");
    if (hex.length === 3) {
        hex = hex.replace(/./g, "$&$&");
    }
    return /^[0-9a-f]{6}$/i.test(hex)
        ? [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16))
        : null;
}

/** The theme's text color blended into its surface color; lightColor otherwise. */
function themeText(weight, lightColor) {
    const text = DARK_THEME && themeRgb("--bs-body-color");
    const surface = DARK_THEME && themeRgb("--surface");
    if (!text || !surface) {
        return lightColor;
    }
    const rgb = text.map((c, i) => Math.round(c * weight + surface[i] * (1 - weight)));
    return `rgb(${rgb.join(", ")})`;
}

/** Chart text and axis colors: the slate palette, or the dark Branded Theme's own. */
export const PROM_COLORS = {
    text: themeText(1, "#0f172a"),
    textSoft: themeText(0.8, "#475569"),
    textMuted: themeText(0.62, "#64748b"),
    axisLine: themeText(0.2, "#e2e8f0"),
};

/** A #rrggbb color as rgba() at the given alpha. */
export function tint(hex, alpha) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function formatNumber(value) {
    const abs = Math.abs(value);
    return value.toFixed(abs >= 100 ? 0 : abs >= 10 ? 1 : 2);
}

/** Prometheus [seconds, "value"] pairs as [milliseconds, number] points, scaled. */
export function toPoints(values, scale = 1) {
    return (values || []).map((pair) => [pair[0] * 1000, parseFloat(pair[1]) * scale]);
}

/** The largest value across several lists of Prometheus [seconds, "value"] pairs. */
export function maxValue(...lists) {
    let max = 0;
    lists.forEach((values) =>
        (values || []).forEach((pair) => {
            const v = parseFloat(pair[1]);
            if (v > max) {
                max = v;
            }
        })
    );
    return max;
}

/** The binary unit that keeps the largest value readable: { divisor, unit }. */
export function byteUnit(max, perSecond = false) {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let index = 0;
    while (index < units.length - 1 && max >= 1024 ** (index + 1)) {
        index += 1;
    }
    return { divisor: 1024 ** index, unit: units[index] + (perSecond ? "/s" : "") };
}

/**
 * Chart options for a time series card.
 * series: [{ name, color, data }]; unit: appended to axis labels and tooltip values.
 */
export function promOptions({ series, unit = "" }) {
    const suffix = unit === "%" ? "%" : unit ? ` ${unit}` : "";
    return {
        title: { text: null },
        credits: { enabled: false },
        chart: {
            type: "area",
            zoomType: "x",
            height: PROM_CHART_HEIGHT,
            backgroundColor: "transparent",
            style: {
                fontFamily: getComputedStyle(document.body).fontFamily,
                fontSize: "12px",
            },
            // [top, right, bottom, left]
            spacing: [10, 12, 6, 8],
        },
        legend: {
            align: "center",
            verticalAlign: "bottom",
            itemStyle: { fontSize: "12px", fontWeight: "600", color: PROM_COLORS.textSoft },
            itemHoverStyle: { color: PROM_COLORS.text },
            symbolRadius: 2,
            padding: 2,
        },
        xAxis: {
            type: "datetime",
            labels: { style: { fontSize: "11px", color: PROM_COLORS.textMuted } },
            lineColor: PROM_COLORS.axisLine,
            tickColor: PROM_COLORS.axisLine,
        },
        yAxis: {
            title: { text: null },
            min: 0,
            maxPadding: 0.05,
            labels: {
                format: `{value}${suffix}`,
                style: { fontSize: "11px", color: PROM_COLORS.textMuted },
            },
            gridLineColor: "rgba(148, 163, 184, 0.25)",
        },
        tooltip: {
            shared: true,
            crosshairs: true,
            formatter: function () {
                const points = this.points || [this];
                const when = new Date(this.x).toLocaleString([], {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                });
                const rows = points.map((point) => {
                    const color = point.color || point.series.color;
                    return (
                        `<span style="color:${color}">●</span> ${point.series.name}: ` +
                        `<b>${formatNumber(point.y)}${suffix}</b>`
                    );
                });
                return `<b>${when}</b><br/>${rows.join("<br/>")}`;
            },
        },
        plotOptions: {
            series: { animation: false, marker: { enabled: false } },
        },
        series: series.map((s) => ({
            animation: false,
            lineWidth: 2,
            marker: { enabled: false },
            fillColor: tint(s.color, 0.1),
            ...s,
        })),
    };
}
