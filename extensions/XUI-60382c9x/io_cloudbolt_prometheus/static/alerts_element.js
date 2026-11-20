import { GraphElement } from "./graph_element.js";

const SEVERITY = {
    critical: { label: "Critical", icon: "bi-exclamation-octagon-fill", pill: "prom-pill-bad" },
    warning: { label: "Warning", icon: "bi-exclamation-triangle-fill", pill: "prom-pill-warn" },
    info: { label: "Info", icon: "bi-info-circle-fill", pill: "prom-pill-info" },
    none: { label: "Alert", icon: "bi-bell-fill", pill: "prom-pill-info" },
};
const PENDING = { label: "Pending", icon: "bi-hourglass-split", pill: "" };

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
        node.className = className;
    }
    if (text) {
        node.textContent = text;
    }
    return node;
}

function pill(style, text) {
    const node = el("span", `prom-pill ${style.pill}`.trim());
    const icon = el("i", `bi ${style.icon}`);
    icon.setAttribute("aria-hidden", "true");
    node.append(icon, document.createTextNode(` ${text}`));
    return node;
}

/** "42 m", "3 h 10 m" or "2 d 4 h" since an RFC 3339 string or seconds since the epoch. */
function elapsed(since) {
    const start = typeof since === "number" ? since * 1000 : Date.parse(since);
    if (!Number.isFinite(start)) {
        return "";
    }
    const minutes = Math.max(0, Math.floor((Date.now() - start) / 60000));
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor((minutes % 1440) / 60);
    if (days) {
        return `${days} d ${hours} h`;
    }
    return hours ? `${hours} h ${minutes % 60} m` : `${minutes} m`;
}

function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/**
 * The alerts card: what is firing or about to fire for this server, most severe first.
 * It also fills the status pill beside the server's name.
 */
export class ServerAlertsElement extends GraphElement {
    get metric() {
        return "alerts";
    }

    async load() {
        let payload;
        try {
            payload = (await this.fetchPayload()) || {};
        } catch (error) {
            this.setStatus(null);
            this.renderCard([el("div", "prom-card-t", "Alerts")], null);
            this.card.appendChild(this.message(`Could not load alerts (${error.message}).`, true));
            return;
        }
        const alerts = payload.alerts || [];
        const firing = alerts.filter((alert) => alert.state === "firing");
        const pending = alerts.filter((alert) => alert.state === "pending");

        if (payload.available === false) {
            this.setStatus(null);
            this.renderCard([el("div", "prom-card-t", "Alerts")], null);
            this.card.appendChild(this.message("Alerts could not be read from the monitoring source.", false));
            return;
        }
        this.setStatus(firing, pending);
        if (!alerts.length) {
            const row = el("div", "prom-alerts-ok");
            const icon = el("i", "bi bi-check-circle-fill");
            icon.setAttribute("aria-hidden", "true");
            row.append(icon, el("span", "prom-alerts-ok-t", "No alerts firing"), el("span", "prom-card-s", "for this server"));
            this.renderCard(null, row);
            return;
        }

        const counts = [];
        if (firing.length) {
            counts.push(`${firing.length} firing`);
        }
        if (pending.length) {
            counts.push(`${pending.length} pending`);
        }
        const list = el("div", "prom-alert-rows");
        alerts.forEach((alert) => list.appendChild(this.row(alert)));
        this.renderCard([el("div", "prom-card-t", "Alerts"), el("div", "prom-card-s", counts.join(", "))], list);
    }

    renderCard(header, body) {
        this.textContent = "";
        this.card = el("section", "prom-card prom-alerts");
        if (header) {
            const head = el("div", "prom-card-h");
            head.append(...header);
            this.card.appendChild(head);
        }
        if (body) {
            this.card.appendChild(body);
        }
        this.appendChild(this.card);
    }

    message(text, isError) {
        const box = el("div", isError ? "prom-alert" : "prom-empty");
        if (isError) {
            const icon = el("i", "bi bi-exclamation-triangle-fill");
            icon.setAttribute("aria-hidden", "true");
            box.appendChild(icon);
        }
        box.appendChild(document.createTextNode(text));
        return box;
    }

    row(alert) {
        const isPending = alert.state === "pending";
        const style = isPending ? PENDING : SEVERITY[alert.severity] || SEVERITY.none;
        const row = el("div", "prom-alert-row");
        row.title = alert.name || "";

        const badge = el("div", "prom-alert-sev");
        badge.appendChild(pill(style, style.label));

        const text = el("div", "prom-alert-text");
        text.appendChild(el("div", "prom-alert-t", alert.title || alert.name));
        if (alert.detail) {
            text.appendChild(el("div", "prom-alert-d", alert.detail));
        }

        const meta = el("div", "prom-alert-meta");
        const duration = elapsed(alert.since);
        if (duration) {
            meta.appendChild(el("div", "", `${isPending ? "True" : "Active"} for ${duration}`));
        }
        if (alert.runbook_url) {
            const link = el("a", "", "Runbook");
            link.href = alert.runbook_url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            meta.appendChild(link);
        }
        row.append(badge, text, meta);
        return row;
    }

    /** The pill beside the server's name; cleared when the state is unknown. */
    setStatus(firing, pending = []) {
        const slot = this.dash && this.dash.querySelector("[data-prom-status]");
        if (!slot) {
            return;
        }
        slot.textContent = "";
        if (!firing) {
            return;
        }
        const critical = firing.filter((alert) => alert.severity === "critical").length;
        const warning = firing.filter((alert) => alert.severity === "warning").length;
        const other = firing.length - critical - warning;
        if (!firing.length) {
            slot.appendChild(
                pending.length
                    ? pill(PENDING, `${pending.length} pending`)
                    : pill({ icon: "bi-check-circle-fill", pill: "prom-pill-ok" }, "Healthy")
            );
            return;
        }
        const parts = [];
        if (critical) {
            parts.push(`${critical} critical`);
        }
        if (warning) {
            parts.push(plural(warning, "warning"));
        }
        if (other) {
            parts.push(plural(other, "alert"));
        }
        const style = critical ? SEVERITY.critical : warning ? SEVERITY.warning : SEVERITY.info;
        slot.appendChild(pill(style, parts.join(", ")));
    }
}
