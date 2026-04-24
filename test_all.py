import os
import shutil
import socket
import threading
import time
from typing import Dict, Optional, Tuple

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from core.node import Node


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _reserve_test_ports(count: int = 3) -> list[int]:
    """Find currently available localhost ports for this integration run."""
    sockets = []
    try:
        ports = []
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("localhost", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        return ports
    finally:
        for sock in sockets:
            sock.close()


def _cleanup_artifacts(ports: list[int]) -> None:
    """Remove only storage directories created for this test run."""
    for port in ports:
        path = os.path.join("afs_storage", f"node_{port}")
        if os.path.isdir(path):
            shutil.rmtree(path)


def _wait_for_leader(nodes: Dict[str, Node], timeout: float = 15.0) -> Tuple[Optional[str], Optional[str]]:
    start = time.time()
    while time.time() - start < timeout:
        leaders = [n.node_id for n in nodes.values() if n.is_leader]
        if len(leaders) == 1:
            leader_id = leaders[0]
            # All followers should agree on leader_id.
            if all(n.leader_id == leader_id or n.node_id == leader_id for n in nodes.values()):
                return leader_id, None
        time.sleep(0.5)
    return None, "Leader election did not converge in time"


def _test_ping(sender: Node, receiver_port: int) -> None:
    reply = sender.send_message(receiver_port, "receive_ping", "integration-test")
    _assert(reply is True, "Ping RPC failed")


def _test_mutual_exclusion(node_a: Node, node_b: Node) -> None:
    intervals = {}

    def contender(label: str, node: Node, hold_time: float) -> None:
        node.request_critical_section()
        entered = time.time()
        time.sleep(hold_time)
        node.release_critical_section()
        exited = time.time()
        intervals[label] = (entered, exited)

    t1 = threading.Thread(target=contender, args=("A", node_a, 2.0), daemon=True)
    t2 = threading.Thread(target=contender, args=("B", node_b, 2.0), daemon=True)

    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    _assert("A" in intervals and "B" in intervals, "Mutual exclusion test threads did not complete")
    a_start, a_end = intervals["A"]
    b_start, b_end = intervals["B"]

    no_overlap = a_end <= b_start or b_end <= a_start
    _assert(no_overlap, "Critical section overlapped; mutual exclusion failed")


def _test_afs(node_a: Node, node_b: Node, node_c: Node) -> None:
    ok = node_a.afs_write("integration.txt", "value-v1")
    _assert(ok, "AFS write v1 failed")

    content = node_b.afs_read("integration.txt")
    _assert(content == "value-v1", f"AFS read mismatch for v1: {content}")

    ok2 = node_c.afs_write("integration.txt", "value-v2")
    _assert(ok2, "AFS write v2 failed")

    content2 = node_a.afs_read("integration.txt")
    _assert(content2 == "value-v2", f"AFS read mismatch for v2: {content2}")


def _test_ai_optional(node: Node) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return "SKIPPED (GEMINI_API_KEY not set)"

    response = node._safe_ai_call('Return ONLY this exact text: AI_OK')
    _assert("AI_OK" in response, f"AI response mismatch: {response}")
    return "PASSED"


def run_all_tests() -> int:
    if load_dotenv is not None:
        load_dotenv()

    print("Starting distributed tests...")

    ports = _reserve_test_ports(3)
    _cleanup_artifacts(ports)

    node_ids = [str(port) for port in ports]
    nodes = {
        node_id: Node(
            node_id=node_id,
            port=port,
            peer_ports=[peer for peer in ports if peer != port],
        )
        for node_id, port in zip(node_ids, ports)
    }

    time.sleep(1.5)

    results = []

    try:
        leader_id, err = _wait_for_leader(nodes)
        _assert(err is None and leader_id is not None, err or "Unknown leader election failure")
        expected_leader = str(max(ports))
        _assert(leader_id == expected_leader, f"Expected leader {expected_leader}, got {leader_id}")
        results.append(("Leader election", "PASSED"))

        _test_ping(nodes[node_ids[0]], ports[1])
        results.append(("Ping/Lamport RPC", "PASSED"))

        _test_mutual_exclusion(nodes[node_ids[0]], nodes[node_ids[1]])
        results.append(("Ricart-Agrawala mutex", "PASSED"))

        _test_afs(nodes[node_ids[0]], nodes[node_ids[1]], nodes[node_ids[2]])
        results.append(("AFS-lite replication", "PASSED"))

        ai_result = _test_ai_optional(nodes[expected_leader])
        results.append(("Gemini connectivity", ai_result))

    except Exception as exc:
        print(f"\nTEST FAILED: {exc}")
        for name, status in results:
            print(f"- {name}: {status}")
        _cleanup_artifacts(ports)
        return 1

    print("\nALL TESTS COMPLETE")
    for name, status in results:
        print(f"- {name}: {status}")
    _cleanup_artifacts(ports)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_all_tests())
