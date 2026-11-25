import {
    ResourceAlertsElement,
    ResourceGraphElement,
    ResourceTableElement,
    ResourceTilesElement,
    ResourceTiersElement,
    clearCache,
} from "./resource_elements.js";

customElements.define("resource-tiers", ResourceTiersElement);
customElements.define("resource-tiles", ResourceTilesElement);
customElements.define("resource-table", ResourceTableElement);
customElements.define("resource-graph", ResourceGraphElement);
customElements.define("resource-alerts", ResourceAlertsElement);

// The tab's Refresh button empties the tab's cache and reloads every card. This module runs once
// per page while CloudBolt may insert the tab's markup again, so the listener sits on the document.
document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest("[data-prom-refresh]");
    const dash = button && button.closest(".io-prom-dash[data-resource-id]");
    if (dash) {
        clearCache(dash);
        dash.dispatchEvent(new CustomEvent("prom-refresh"));
    }
});
