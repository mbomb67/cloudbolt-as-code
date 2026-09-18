// import {GraphElement} from './graph_element.js';
import {CpuLoadGraphElement} from './cpu_load_element.js';
import {NodeHealthElement} from "./node_health_element.js";
import {MemUsageElement} from "./mem_usage_element.js";
import {DiskUsageGraphElement} from "./disk_usage_element.js";
import {DiskIOGraphElement} from "./disk_io_element.js";
import {IopsGraphElement} from "./iops_element.js";
import {NetIOGraphElement} from "./net_io_element.js";


customElements.define('node-health', NodeHealthElement);
customElements.define('cpu-load-graph', CpuLoadGraphElement);
customElements.define('mem-usage-graph', MemUsageElement);
customElements.define('disk-usage-graph', DiskUsageGraphElement);
customElements.define('disk-io-graph', DiskIOGraphElement);
customElements.define('iops-graph', IopsGraphElement);
customElements.define('net-io-graph', NetIOGraphElement);
