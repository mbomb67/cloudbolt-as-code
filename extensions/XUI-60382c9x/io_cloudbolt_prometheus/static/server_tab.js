import { CpuLoadGraphElement } from "./cpu_load_element.js";
import { NodeHealthElement } from "./node_health_element.js";
import { MemUsageElement } from "./mem_usage_element.js";
import { DiskUsageGraphElement } from "./disk_usage_element.js";
import { DiskIOGraphElement } from "./disk_io_element.js";
import { IopsGraphElement } from "./iops_element.js";
import { NetIOGraphElement } from "./net_io_element.js";

customElements.define("node-health", NodeHealthElement);
customElements.define("cpu-load-graph", CpuLoadGraphElement);
customElements.define("mem-usage-graph", MemUsageElement);
customElements.define("disk-usage-graph", DiskUsageGraphElement);
customElements.define("disk-io-graph", DiskIOGraphElement);
customElements.define("iops-graph", IopsGraphElement);
customElements.define("net-io-graph", NetIOGraphElement);

// The tab's Refresh button reloads every card. This module runs once per page while
// CloudBolt may insert the tab's markup again, so the listener sits on the document.
document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest("[data-prom-refresh]");
    const dash = button && button.closest(".io-prom-dash");
    if (dash) {
        dash.dispatchEvent(new CustomEvent("prom-refresh"));
    }
});
