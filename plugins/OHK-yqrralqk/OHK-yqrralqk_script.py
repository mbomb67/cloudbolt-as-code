"""Server action: runs a shell script on an OpenShift Virtualization VM over SSH tunnelled through the cluster API."""

import io
import socket
import ssl

import paramiko
import websocket

from common.methods import find_key_material, set_progress

SCRIPT = """{{ script }}"""
TIMEOUT_SECONDS = 600


class ApiTunnel:
    """Socket for paramiko backed by the VM's port-forward websocket; works without a routable guest IP."""

    def __init__(self, server):
        handler = server.resource_handler.cast()
        client = handler.get_api_wrapper().client
        client._login()
        verify = client.http_session.verify
        self.buffer = b""
        self.websocket = websocket.create_connection(
            f"wss://api.{handler.ip}:{handler.port}/apis/subresources.kubevirt.io/v1"
            f"/namespaces/{server.ovirtserverinfo.namespace}"
            f"/virtualmachineinstances/{server.hostname}/portforward/22/tcp",
            header=[f"Authorization: {client.http_session.headers['Authorization']}"],
            subprotocols=["plain.kubevirt.io"],
            sslopt={"cert_reqs": ssl.CERT_NONE} if verify is False else {},
            timeout=30,
        )

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


def run(job, server=None, **kwargs):
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

    set_progress(f"Opening the API tunnel to {server.hostname} as {username}")
    transport = paramiko.Transport(ApiTunnel(server))
    try:
        transport.connect(username=username, password=None if key else password, pkey=key)
        channel = transport.open_session()
        channel.settimeout(TIMEOUT_SECONDS)
        channel.set_combine_stderr(True)
        channel.exec_command("bash -s")
        channel.sendall(SCRIPT.replace("\r\n", "\n"))
        channel.shutdown_write()
        output = channel.makefile().read().decode(errors="replace")
        exit_code = channel.recv_exit_status()
    finally:
        transport.close()

    set_progress(output)
    if exit_code:
        return "FAILURE", output, f"The script exited with code {exit_code}"
    return "SUCCESS", output, ""
