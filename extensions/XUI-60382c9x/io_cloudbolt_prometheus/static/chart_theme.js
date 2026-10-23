/**
 * Shared Highcharts styling for the monitoring tab (compact, credits as chart title).
 */
export const PROM_CHART_HEIGHT = 210;

const DARK_THEME = document.documentElement.getAttribute("data-bs-theme") === "dark";

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

export function promCredits(chartTitle) {
    return {
        enabled: true,
        text: chartTitle,
        href: null,
        position: { align: "right", x: -6, y: -4 },
        style: {
            color: PROM_COLORS.textMuted,
            fontSize: "11px",
            fontWeight: "600",
            cursor: "default",
        },
    };
}

export function promChartCommon(overrides = {}) {
    return {
        backgroundColor: "transparent",
        style: {
            fontFamily:
                'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
            fontSize: "11px",
        },
        // [top, right, bottom, left] — top/left must leave room or Y-axis labels clip
        spacing: [16, 12, 10, 12],
        ...overrides,
    };
}

export const promLegendCompact = {
    align: "center",
    verticalAlign: "bottom",
    itemStyle: {
        fontSize: "10px",
        fontWeight: "500",
        color: PROM_COLORS.textSoft,
    },
    itemHoverStyle: { color: PROM_COLORS.text },
    itemMarginTop: 0,
    itemMarginBottom: 0,
    symbolRadius: 2,
    padding: 2,
};

export const promXAxisDatetime = {
    type: "datetime",
    labels: { style: { fontSize: "10px", color: PROM_COLORS.textMuted } },
    lineColor: PROM_COLORS.axisLine,
    tickColor: PROM_COLORS.axisLine,
    gridLineColor: "rgba(148, 163, 184, 0.25)",
};

export const promYAxisCompact = {
    title: { text: null },
    maxPadding: 0.02,
    labels: {
        style: { fontSize: "10px", color: PROM_COLORS.textMuted },
        x: -2,
    },
    gridLineColor: "rgba(148, 163, 184, 0.2)",
};
