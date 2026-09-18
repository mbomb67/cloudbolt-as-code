/**
 * Shared Highcharts styling for the monitoring tab (compact, credits as chart title).
 */
export const PROM_CHART_HEIGHT = 210;

export function promCredits(chartTitle) {
    return {
        enabled: true,
        text: chartTitle,
        href: null,
        position: { align: "right", x: -6, y: -4 },
        style: {
            color: "#64748b",
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
        color: "#475569",
    },
    itemHoverStyle: { color: "#0f172a" },
    itemMarginTop: 0,
    itemMarginBottom: 0,
    symbolRadius: 2,
    padding: 2,
};

export const promXAxisDatetime = {
    type: "datetime",
    labels: { style: { fontSize: "10px", color: "#64748b" } },
    lineColor: "#e2e8f0",
    tickColor: "#e2e8f0",
    gridLineColor: "rgba(148, 163, 184, 0.25)",
};

export const promYAxisCompact = {
    title: { text: null },
    maxPadding: 0.02,
    labels: {
        style: { fontSize: "10px", color: "#64748b" },
        x: -2,
    },
    gridLineColor: "rgba(148, 163, 184, 0.2)",
};
