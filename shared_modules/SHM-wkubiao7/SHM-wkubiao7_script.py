"""SSH and raw TCP to an OpenShift Virtualization VM, tunnelled through the cluster API.

The tunnel is KubeVirt's port-forward subresource, which reaches the VM only through its pod network
interface, so CloudBolt needs no route to the guest but the VM needs a pod network (masquerade) NIC
as its first interface.

    from shared_modules import kubevirt_api_tunnel

    exit_code, output = kubevirt_api_tunnel.run_script(server, "hostname")   # one script over SSH
    transport = kubevirt_api_tunnel.connect_ssh(server)                      # paramiko.Transport
    sock = kubevirt_api_tunnel.ApiTunnel(server, port=5432)                  # any guest TCP port

Every function raises TunnelError, with a message the requester can act on, when the tunnel cannot
be opened. Close what you open: transport.close() or sock.close().
"""

import io
import socket
import ssl

import paramiko
import websocket

from common.methods import find_key_material
from utilities.exceptions import NotFoundException

SSH_PORT = 22
HANDSHAKE_TIMEOUT_SECONDS = 30
SCRIPT_TIMEOUT_SECONDS = 600


class TunnelError(Exception):
    """The API tunnel could not be opened; the message says why in terms the requester can act on."""


def check_port_forward_target(client, namespace, name):
    """Raise TunnelError when KubeVirt cannot port-forward to this VM; return the address it will dial.

    virt-api opens a TCP connection to the first interface in the VMI's status before it answers the
    websocket upgrade, and that dial has no timeout, so a VM whose first interface is not on the pod
    network makes the handshake hang instead of failing.
    https://github.com/kubevirt/kubevirt/blob/release-1.6/pkg/virt-api/rest/dialers.go
    https://kubevirt.io/user-guide/user_workloads/accessing_virtual_machines/
    """
    try:
        vmi = client.send_request(
            "GET", f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}"
        )
    except NotFoundException:
        raise TunnelError(f"{name} is not running in namespace {namespace}, so there is nothing to connect to.")

    phase = vmi.get("status", {}).get("phase")
    if phase != "Running":
        raise TunnelError(f"{name} is in phase {phase}; the API tunnel needs a running VM.")

    networks = vmi.get("spec", {}).get("networks", [])
    pod_network = next((network["name"] for network in networks if "pod" in network), None)
    if not pod_network:
        attached = ", ".join(
            network.get("multus", {}).get("networkName", network["name"]) for network in networks
        )
        raise TunnelError(
            f"{name} has no pod network interface (it is attached to: {attached or 'no network'}). "
            "KubeVirt port-forward reaches a VM only through the pod network, so add a pod network "
            "(masquerade) NIC as the VM's first interface."
        )

    interfaces = vmi.get("status", {}).get("interfaces", [])
    if not interfaces or not interfaces[0].get("ipAddress"):
        raise TunnelError(f"{name} has not reported an IP address on its first interface yet.")
    if interfaces[0].get("name") != pod_network:
        raise TunnelError(
            f"The first interface of {name} is {interfaces[0].get('name')}, not its pod network "
            f"interface {pod_network}. KubeVirt port-forward dials the first interface, so the pod "
            "network NIC must come first."
        )
    return interfaces[0]["ipAddress"]


class ApiTunnel:
    """A TCP connection to a port on the VM, carried by the port-forward websocket.

    Implements the part of the socket interface paramiko needs: send, recv, settimeout, close.
    """

    def __init__(self, server, port=SSH_PORT):
        handler = server.resource_handler.cast()
        client = handler.get_api_wrapper().client
        client._login()
        namespace = server.ovirtserverinfo.namespace
        target = check_port_forward_target(client, namespace, server.hostname)

        # The session's verify is False or the path of CloudBolt's CA bundle.
        verify = client.http_session.verify
        if verify is False:
            sslopt = {"cert_reqs": ssl.CERT_NONE}
        elif isinstance(verify, str):
            sslopt = {"ca_certs": verify}
        else:
            sslopt = {}

        self.buffer = b""
        try:
            self.websocket = websocket.create_connection(
                f"wss://api.{handler.ip}:{handler.port}/apis/subresources.kubevirt.io/v1"
                f"/namespaces/{namespace}"
                f"/virtualmachineinstances/{server.hostname}/portforward/{port}/tcp",
                header=[f"Authorization: {client.http_session.headers['Authorization']}"],
                subprotocols=["plain.kubevirt.io"],
                sslopt=sslopt,
                timeout=HANDSHAKE_TIMEOUT_SECONDS,
            )
        except websocket.WebSocketTimeoutException:
            raise TunnelError(
                f"The cluster never answered the port-forward request, which means its connection to "
                f"{target}:{port} is being dropped. Check that the service is listening and the guest "
                f"firewall allows port {port}, and that no NetworkPolicy in namespace {namespace} blocks "
                "the virt-api pods."
            )
        except websocket.WebSocketBadStatusException as error:
            body = (error.resp_body or b"").decode(errors="replace")
            raise TunnelError(f"The cluster refused the port-forward request ({error.status_code}): {body}")

    def send(self, data):
        self.websocket.send_binary(data)
        return len(data)

    def recv(self, size):
        if not self.buffer:
            try:
                self.buffer = self.websocket.recv()
            except websocket.WebSocketTimeoutException:
                raise socket.timeout()
            except websocket.WebSocketConnectionClosedException:
                return b""
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def settimeout(self, seconds):
        self.websocket.settimeout(seconds)

    def close(self):
        self.websocket.close()


def connect_ssh(server, port=SSH_PORT):
    """Return an authenticated paramiko.Transport to the VM, using the server's CloudBolt credentials."""
    username, password, key_name, key_location = server.credentials_for_script()
    key = None
    if key_name:
        material = find_key_material(key_name, key_location)[1]
        # The stored key's type is not recorded, so try each one paramiko reads.
        for key_class in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
            try:
                key = key_class.from_private_key(io.StringIO(material))
                break
            except paramiko.SSHException:
                continue

    transport = paramiko.Transport(ApiTunnel(server, port))
    try:
        transport.connect(username=username, password=None if key else password, pkey=key)
    except Exception:
        transport.close()
        raise
    return transport


def run_script(server, script, interpreter="bash -s", timeout=SCRIPT_TIMEOUT_SECONDS):
    """Run a script on the VM over SSH; return (exit_code, output) with stderr folded into the output."""
    transport = connect_ssh(server)
    try:
        channel = transport.open_session()
        channel.settimeout(timeout)
        channel.set_combine_stderr(True)
        channel.exec_command(interpreter)
        channel.sendall(script.replace("\r\n", "\n"))
        channel.shutdown_write()
        output = channel.makefile().read().decode(errors="replace")
        return channel.recv_exit_status(), output
    finally:
        transport.close()
