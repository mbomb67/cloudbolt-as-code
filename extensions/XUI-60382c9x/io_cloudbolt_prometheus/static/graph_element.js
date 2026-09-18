export class GraphElement extends HTMLElement {

    connectedCallback() {
        const server_id = this.getAttribute("server-id");
        console.log(this.constructor.name + ":Graph connected");
        this.load(server_id);
    }

    async load(server_id) {
        console.log(this.constructor.name + ": Load data for server_id: " + server_id);
        await this.render_component();
    }

    render_component(payload) {
        console.log("Render component for: " + this.constructor.name);
    }

}
