import threading
from xmlrpc.server import SimpleXMLRPCServer
from socketserver import ThreadingMixIn  # Add this import
import xmlrpc.client
import time
import json
import os
import hashlib
import socket
from google import genai
from google.genai import types


class ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    """Allows the XML-RPC server to handle requests concurrently."""
    pass


class TimeoutTransport(xmlrpc.client.Transport):
    """Sets a per-request socket timeout for XML-RPC calls."""

    def __init__(self, timeout_seconds):
        super().__init__()
        self.timeout_seconds = timeout_seconds

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout_seconds
        return conn


try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


class Node:
    def __init__(self, node_id, port, peer_ports, liveness_timeout=10.0):
        self.node_id = node_id
        self.port = port
        self.peer_ports = peer_ports
        self.liveness_timeout = float(liveness_timeout)
        self.ai_client = None
        self.last_ai_error = None

        # Phase 2: Lamport Logical Clock
        self.logical_clock = 0
        self.clock_lock = threading.Lock()

        # Phase 3: Leader Election State
        self.leader_id = None
        self.is_leader = False
        self.election_in_progress = False
        self.election_lock = threading.Lock()
        self.election_round = 0
        self.peer_lock = threading.Lock()
        self.peer_last_seen = {peer: time.time() for peer in self.peer_ports}

        # Start the RPC Server
        self.server_thread = threading.Thread(
            target=self._start_server, daemon=True)
        self.server_thread.start()

        # Bootstrap leader election once at startup with no ongoing liveness traffic.
        threading.Thread(target=self._bootstrap_election, daemon=True).start()

        # Phase 4: Ricart-Agrawala DME State
        self.cs_state = 'RELEASED'
        self.cs_request_timestamp = 0
        self.cs_replies_received = 0
        self.cs_expected_replies = 0
        self.cs_pending_replies = set()
        self.deferred_requests = []
        self.cs_lock = threading.Lock()  # Protects DME state from race conditions

        # AFS-lite state: replicated storage + client cache
        self.afs_lock = threading.Lock()
        self.afs_cache = {}
        self.afs_index = {}
        self.replication_factor = min(3, len(self.peer_ports) + 1)
        self.afs_storage_dir = os.path.join("afs_storage", f"node_{self.port}")
        os.makedirs(self.afs_storage_dir, exist_ok=True)

        # Phase 5: AI Integration
        self._load_env()
        try:
            if not os.getenv("GEMINI_API_KEY"):
                raise ValueError("GEMINI_API_KEY is not set.")
            self.ai_client = genai.Client()
            self.ai_model = 'gemini-2.5-flash'
        except Exception as e:
            print(
                f"[Node {self.node_id}] Warning: AI client failed to initialize. {e}")

    def _load_env(self):
        """Loads environment variables from a local .env file if available."""
        if load_dotenv is None:
            print(
                f"[Node {self.node_id}] Warning: python-dotenv not installed; skipping .env load.")
            return

        dotenv_path = os.path.join(os.getcwd(), ".env")
        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path=dotenv_path)

    def _start_server(self):
        """Phase 1: Initialize the XML-RPC server to listen for incoming messages."""
        # Replace SimpleXMLRPCServer with ThreadedXMLRPCServer
        server = ThreadedXMLRPCServer(
            ("localhost", self.port), logRequests=False, allow_none=True)
        server.register_instance(self)
        print(f"[Node {self.node_id}] Listening on port {self.port}...")
        server.serve_forever()

    # --- Phase 2: Lamport Clock Operations ---

    def tick(self):
        """Increment clock for an internal event."""
        with self.clock_lock:
            self.logical_clock += 1
            return self.logical_clock

    def sync_clock(self, incoming_clock):
        """Update clock based on a received message's timestamp."""
        with self.clock_lock:
            self.logical_clock = max(self.logical_clock, incoming_clock) + 1
            return self.logical_clock

    # --- Phase 1: Network Communication ---

    def _bootstrap_election(self):
        """Run one election after startup if no leader has been announced yet."""
        time.sleep(5)
        if self.is_leader or self.leader_id is not None:
            return
        print(
            f"[Node {self.node_id}] No leader announced yet. Starting election.")
        self.start_election()

    def _mark_peer_alive(self, peer_port):
        with self.peer_lock:
            self.peer_last_seen[int(peer_port)] = time.time()

    def _mark_peer_dead(self, peer_port, reason=None):
        with self.peer_lock:
            self.peer_last_seen[int(peer_port)] = 0.0
        if reason:
            print(f"[Network] Node {peer_port} considered DOWN ({reason}).")
        else:
            print(f"[Network] Node {peer_port} considered DOWN.")

    def _leader_port(self):
        if self.leader_id is None:
            return None
        try:
            return int(self.leader_id)
        except (TypeError, ValueError):
            return None

    def _request_cs_permission(self, peer_port, req_timestamp):
        """Send one RA request and stop waiting for peers that time out immediately."""
        reply = self.send_message(
            peer_port, "receive_cs_request", req_timestamp)
        if reply is None:
            with self.cs_lock:
                self.cs_pending_replies.discard(int(peer_port))
            self._mark_peer_dead(
                peer_port, f"no CS request ACK within {self.liveness_timeout}s")

    def _await_coordinator(self, round_id):
        """If no coordinator arrives in time, restart election for this node."""
        time.sleep(self.liveness_timeout)

        with self.election_lock:
            if self.election_round != round_id or not self.election_in_progress or self.is_leader:
                return
            self.election_in_progress = False
            self.leader_id = None

        print(
            f"[Node {self.node_id}] Election round {round_id} timed out after {self.liveness_timeout}s. Retrying election.")
        self.start_election()

    def send_message(self, target_port, method_name, *args):
        """Helper to send RPC calls to other nodes, attaching the current clock."""
        current_time = self.tick()  # Tick before sending
        target_url = f"http://localhost:{target_port}"
        transport = TimeoutTransport(self.liveness_timeout)

        try:
            with xmlrpc.client.ServerProxy(target_url, transport=transport, allow_none=True) as proxy:
                method = getattr(proxy, method_name)
                result = method(current_time, self.node_id, *args)
                self._mark_peer_alive(target_port)
                return result

        except (ConnectionRefusedError, socket.timeout, OSError):
            self._mark_peer_dead(
                target_port, f"RPC {method_name} timeout/refused")
            return None

    # --- RPC Exposed Methods (Callable by other nodes) ---

    def receive_ping(self, sender_clock, sender_id, message):
        """A simple method to test connectivity and clock syncing."""
        updated_time = self.sync_clock(sender_clock)
        self._mark_peer_alive(int(sender_id))
        print(
            f"\n[Node {self.node_id}] Received Ping from Node {sender_id}: '{message}'")
        print(
            f"[Node {self.node_id}] Clock Synced: Sender({sender_clock}) -> Local({updated_time})")
        return True

    # --- Phase 3: Heartbeat & Monitoring ---

    def _heartbeat_loop(self):
        """Leader exclusively runs this to let others know it is alive."""
        while True:
            if self.is_leader:
                for peer in self.peer_ports:
                    # FIRE AND FORGET: Spawns a micro-thread for each heartbeat
                    # so a slow peer doesn't block the next peer's heartbeat.
                    threading.Thread(
                        target=self.send_message,
                        args=(peer, "receive_heartbeat"),
                        daemon=True
                    ).start()
            time.sleep(2)  # Send a heartbeat every 2 seconds

    def _monitor_leader(self):
        """Followers run this to detect if the leader has crashed."""
        time.sleep(5)  # Deprecated by timeout-on-demand liveness checks.
        while True:
            time.sleep(10)
    # --- Phase 3: Bully Algorithm Logic ---

    def start_election(self):
        with self.election_lock:
            self.election_in_progress = True
            self.is_leader = False
            self.leader_id = None
            self.election_round += 1
            round_id = self.election_round

        # Find peers with a higher ID (port number)
        higher_nodes = [p for p in self.peer_ports if p > self.port]

        if not higher_nodes:
            # I am the highest ID. I win by default.
            self.become_leader()
            return

        # Send an ELECTION message to all higher nodes
        got_ok = False
        for peer in higher_nodes:
            reply = self.send_message(peer, "receive_election")
            if reply:  # An active higher node responded 'OK'
                got_ok = True

        if not got_ok:
            # Higher nodes exist, but none responded. They must be dead. I win.
            self.become_leader()
        else:
            # A higher node responded. Wait for coordinator announcement with timeout.
            print(
                f"[Node {self.node_id}] Higher node responded. Waiting up to {self.liveness_timeout}s for coordinator.")
            threading.Thread(
                target=self._await_coordinator,
                args=(round_id,),
                daemon=True
            ).start()

    def become_leader(self):
        print(f"\n*** [Node {self.node_id}] I am the new LEADER! ***")
        with self.election_lock:
            self.is_leader = True
            self.leader_id = self.node_id
            self.election_in_progress = False

        # Broadcast the victory to lower nodes
        lower_nodes = [p for p in self.peer_ports if p < self.port]
        for peer in lower_nodes:
            self.send_message(peer, "receive_coordinator")

    # --- Phase 3: RPC Exposed Methods for Election ---

    def receive_heartbeat(self, sender_clock, sender_id):
        """Called by the Leader."""
        self.sync_clock(sender_clock)
        self._mark_peer_alive(int(sender_id))

        # If a new node joins and starts sending heartbeats, accept it
        if self.leader_id != sender_id:
            print(
                f"[Node {self.node_id}] Recognizing new leader: Node {sender_id}")
            self.leader_id = sender_id

        self.is_leader = False
        self.election_in_progress = False
        return True

    def receive_election(self, sender_clock, sender_id):
        """Called by a lower-ID node starting an election."""
        self.sync_clock(sender_clock)
        self._mark_peer_alive(int(sender_id))
        print(
            f"[Node {self.node_id}] Received ELECTION from {sender_id}. Replying OK.")

        # If we aren't already running an election, start one to challenge higher nodes
        if not self.election_in_progress:
            threading.Thread(target=self.start_election, daemon=True).start()
        return True  # Returning True acts as the "OK" message

    def receive_coordinator(self, sender_clock, sender_id):
        """Called by the new Leader declaring victory."""
        self.sync_clock(sender_clock)
        self._mark_peer_alive(int(sender_id))
        print(
            f"\n[Node {self.node_id}] Node {sender_id} has declared itself COORDINATOR.")
        with self.election_lock:
            self.leader_id = sender_id
            self.is_leader = False
            self.election_in_progress = False
        return True
    # --- Phase 4: Ricart-Agrawala Core Logic ---

    def request_critical_section(self):
        """Called locally when the node wants to write to the shared resource."""
        with self.cs_lock:
            self.cs_state = 'WANTED'
            self.cs_request_timestamp = self.tick()
            self.cs_replies_received = 0
            self.cs_expected_replies = len(self.peer_ports)
            self.cs_pending_replies = set(self.peer_ports)
            self.deferred_requests.clear()

        print(
            f"\n[Node {self.node_id}] Requesting Critical Section at TS {self.cs_request_timestamp}")

        # If no peers exist, we get the lock instantly.
        if not self.peer_ports:
            with self.cs_lock:
                self.cs_state = 'HELD'
            print(
                f"\n*** [Node {self.node_id}] Entered Critical Section (Solo Node)! ***")
            return

        # Broadcast request to all peers and wait for replies until timeout.
        for peer in self.peer_ports:
            threading.Thread(
                target=self._request_cs_permission,
                args=(peer, self.cs_request_timestamp),
                daemon=True
            ).start()

        deadline = time.time() + self.liveness_timeout
        while True:
            with self.cs_lock:
                if not self.cs_pending_replies:
                    self.cs_state = 'HELD'
                    break

            if time.time() >= deadline:
                with self.cs_lock:
                    timed_out = sorted(self.cs_pending_replies)
                    self.cs_pending_replies.clear()
                    self.cs_state = 'HELD'
                if timed_out:
                    print(
                        f"[Node {self.node_id}] CS wait timed out ({self.liveness_timeout}s). Proceeding without replies from: {timed_out}")
                    for peer in timed_out:
                        self._mark_peer_dead(
                            peer, f"no CS reply within {self.liveness_timeout}s")
                break
            time.sleep(0.1)

        print(f"\n*** [Node {self.node_id}] Entered Critical Section! ***")
        # In the final app, file writing happens here.

    def release_critical_section(self):
        """Called locally when the node finishes writing."""
        print(f"*** [Node {self.node_id}] Leaving Critical Section. ***")
        with self.cs_lock:
            self.cs_state = 'RELEASED'
            # Send a reply to everyone we kept waiting
            for deferred_id in self.deferred_requests:
                peer_port = int(deferred_id)
                print(
                    f"[Node {self.node_id}] Sending deferred reply to Node {deferred_id}")
                threading.Thread(
                    target=self.send_message,
                    args=(peer_port, "receive_cs_reply"),
                    daemon=True
                ).start()
            self.deferred_requests.clear()
    # --- Phase 4: DME RPC Methods ---

    def receive_cs_request(self, sender_clock, sender_id, req_timestamp):
        """Handles an incoming request for the Critical Section."""
        self.sync_clock(sender_clock)
        self._mark_peer_alive(int(sender_id))

        with self.cs_lock:
            # Ricart-Agrawala priority check
            we_have_priority = False
            if self.cs_state == 'WANTED':
                # Lower timestamp wins. If tied, lower Node ID wins.
                if self.cs_request_timestamp < req_timestamp:
                    we_have_priority = True
                elif self.cs_request_timestamp == req_timestamp and int(self.node_id) < int(sender_id):
                    we_have_priority = True

            # If we are using the resource, or we want it and have priority, DEFER
            if self.cs_state == 'HELD' or we_have_priority:
                print(
                    f"[Node {self.node_id}] Deferring CS request from Node {sender_id}")
                self.deferred_requests.append(sender_id)
            else:
                # We don't care, or they have priority. REPLY immediately.
                peer_port = int(sender_id)
                print(
                    f"[Node {self.node_id}] Sending immediate CS reply to Node {sender_id}")
                threading.Thread(
                    target=self.send_message,
                    args=(peer_port, "receive_cs_reply"),
                    daemon=True
                ).start()
        return True

    def receive_cs_reply(self, sender_clock, sender_id):
        """Handles an incoming reply granting permission."""
        self.sync_clock(sender_clock)
        sender_port = int(sender_id)
        self._mark_peer_alive(sender_port)
        with self.cs_lock:
            if sender_port in self.cs_pending_replies:
                self.cs_pending_replies.remove(sender_port)
            self.cs_replies_received = self.cs_expected_replies - \
                len(self.cs_pending_replies)
            print(
                f"[Node {self.node_id}] Received CS permission from Node {sender_id} ({self.cs_replies_received}/{self.cs_expected_replies})")
        return True

    # --- AFS-lite: Fault-tolerant replicated file storage ---

    def _sanitize_afs_filename(self, filename):
        return os.path.basename(filename.strip())

    def _replica_path(self, filename):
        safe_name = self._sanitize_afs_filename(filename)
        return os.path.join(self.afs_storage_dir, f"{safe_name}.afs.json")

    def _all_node_ports(self):
        return sorted([self.port] + self.peer_ports)

    def _replica_ports(self, filename):
        all_ports = self._all_node_ports()
        if not all_ports:
            return []

        total = min(self.replication_factor, len(all_ports))
        digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()
        start_index = int(digest, 16) % len(all_ports)
        ports = []
        for i in range(total):
            ports.append(all_ports[(start_index + i) % len(all_ports)])
        return ports

    def _local_store_replica(self, filename, content, version):
        safe_name = self._sanitize_afs_filename(filename)
        payload = {
            "filename": safe_name,
            "version": int(version),
            "content": content,
            "updated_at": time.time(),
        }

        path = self._replica_path(safe_name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        print(
            f"[AFS][Node {self.node_id}] Stored local replica: {safe_name} v{int(version)} -> {path}")

        with self.afs_lock:
            self.afs_index[safe_name] = {
                "version": int(version),
                "replicas": self._replica_ports(safe_name),
                "updated_at": payload["updated_at"],
            }
            self.afs_cache[safe_name] = {
                "version": int(version),
                "content": content,
            }

    def _local_fetch_replica(self, filename):
        safe_name = self._sanitize_afs_filename(filename)
        path = self._replica_path(safe_name)
        if not os.path.exists(path):
            print(
                f"[AFS][Node {self.node_id}] Local replica miss: {safe_name}")
            return {"ok": False}

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            print(
                f"[AFS][Node {self.node_id}] Local replica fetch: {safe_name} v{int(payload.get('version', 0))}")
            return {
                "ok": True,
                "filename": safe_name,
                "version": int(payload.get("version", 0)),
                "content": payload.get("content", ""),
            }
        except Exception:
            print(
                f"[AFS][Node {self.node_id}] Failed to parse local replica: {safe_name}")
            return {"ok": False}

    def afs_write(self, filename, content):
        """Client entrypoint: quorum write with replication and invalidation."""
        safe_name = self._sanitize_afs_filename(filename)
        if not safe_name:
            print(f"[Node {self.node_id}] Invalid filename.")
            return False

        replicas = self._replica_ports(safe_name)
        if not replicas:
            print(f"[Node {self.node_id}] No replicas available for write.")
            return False

        print(
            f"[AFS][Node {self.node_id}] WRITE start: {safe_name} replicas={replicas}")

        self.request_critical_section()
        success = False
        try:
            latest = self._read_latest_record(safe_name)
            new_version = latest["version"] + 1 if latest else 1
            print(
                f"[AFS][Node {self.node_id}] WRITE version chosen: {safe_name} v{new_version}")

            required_acks = (len(replicas) // 2) + 1
            ack_count = 0

            for peer in replicas:
                if peer == self.port:
                    print(
                        f"[AFS][Node {self.node_id}] WRITE local store on Node {self.port}: {safe_name} v{new_version}")
                    self._local_store_replica(safe_name, content, new_version)
                    ack_count += 1
                else:
                    print(
                        f"[AFS][Node {self.node_id}] WRITE sending replica to Node {peer}: {safe_name} v{new_version}")
                    ack = self.send_message(
                        peer, "receive_afs_store_replica", safe_name, content, new_version)
                    if ack:
                        ack_count += 1
                        print(
                            f"[AFS][Node {self.node_id}] WRITE ack from Node {peer}: {safe_name} v{new_version}")
                    else:
                        print(
                            f"[AFS][Node {self.node_id}] WRITE no ack from Node {peer}: {safe_name} v{new_version}")

            if ack_count >= required_acks:
                for peer in self.peer_ports:
                    self.send_message(
                        peer, "receive_afs_invalidate", safe_name, new_version)
                print(
                    f"[Node {self.node_id}] AFS write committed: {safe_name} v{new_version} ({ack_count}/{len(replicas)} acks)")
                success = True
            else:
                print(
                    f"[Node {self.node_id}] AFS write failed quorum: {safe_name} ({ack_count}/{len(replicas)} acks)")
        finally:
            self.release_critical_section()

        return success

    def afs_read(self, filename):
        """Client entrypoint: read latest version from replicas and repair stale copies."""
        safe_name = self._sanitize_afs_filename(filename)
        if not safe_name:
            print(f"[Node {self.node_id}] Invalid filename.")
            return None

        replicas = self._replica_ports(safe_name)
        print(
            f"[AFS][Node {self.node_id}] READ start: {safe_name} replicas={replicas}")

        latest = self._read_latest_record(safe_name)
        if not latest:
            print(f"[Node {self.node_id}] AFS read miss: {safe_name}")
            return None

        # Best-effort read repair for missing or stale replicas.
        for peer in replicas:
            if peer == self.port:
                local = self._local_fetch_replica(safe_name)
                if (not local.get("ok")) or int(local.get("version", 0)) < latest["version"]:
                    print(
                        f"[AFS][Node {self.node_id}] READ repair local replica: {safe_name} -> v{latest['version']}")
                    self._local_store_replica(
                        safe_name, latest["content"], latest["version"])
            else:
                print(
                    f"[AFS][Node {self.node_id}] READ repair send to Node {peer}: {safe_name} v{latest['version']}")
                self.send_message(peer, "receive_afs_store_replica",
                                  safe_name, latest["content"], latest["version"])

        with self.afs_lock:
            self.afs_cache[safe_name] = {
                "version": latest["version"],
                "content": latest["content"],
            }

        print(
            f"[Node {self.node_id}] AFS read: {safe_name} v{latest['version']}")
        return latest["content"]

    def _read_latest_record(self, filename):
        replicas = self._replica_ports(filename)
        candidates = []
        print(
            f"[AFS][Node {self.node_id}] READ-LATEST querying replicas for {filename}: {replicas}")

        for peer in replicas:
            if peer == self.port:
                data = self._local_fetch_replica(filename)
            else:
                print(
                    f"[AFS][Node {self.node_id}] READ-LATEST fetch request to Node {peer}: {filename}")
                data = self.send_message(peer, "receive_afs_fetch", filename)

            if data and isinstance(data, dict) and data.get("ok"):
                candidates.append(data)
                print(
                    f"[AFS][Node {self.node_id}] READ-LATEST candidate: {filename} v{int(data.get('version', 0))}")

        if not candidates:
            print(
                f"[AFS][Node {self.node_id}] READ-LATEST no candidates for {filename}")
            return None

        return max(candidates, key=lambda item: int(item.get("version", 0)))

    def afs_status(self):
        with self.afs_lock:
            print(f"\n[Node {self.node_id}] AFS status")
            print(f"- Local storage: {self.afs_storage_dir}")
            print(f"- Replication factor: {self.replication_factor}")
            print(f"- Cached files: {len(self.afs_cache)}")
            print(f"- Indexed files: {len(self.afs_index)}")
            for name, meta in self.afs_index.items():
                print(
                    f"  * {name} v{meta['version']} replicas={meta['replicas']}")

    def receive_afs_store_replica(self, sender_clock, sender_id, filename, content, version):
        self.sync_clock(sender_clock)
        safe_name = self._sanitize_afs_filename(filename)
        print(
            f"[AFS][Node {self.node_id}] RPC store replica from Node {sender_id}: {safe_name} v{int(version)}")
        current = self._local_fetch_replica(safe_name)

        if current.get("ok") and int(current.get("version", 0)) > int(version):
            print(
                f"[AFS][Node {self.node_id}] RPC store skipped (newer local exists): {safe_name} local_v{int(current.get('version', 0))} incoming_v{int(version)}")
            return True

        self._local_store_replica(safe_name, content, int(version))

        # --- Call the new safe invalidation method here ---
        # This replaces the old inline loop that blocked the lock!
        self._invalidate_callbacks(safe_name, version)
        return True

    # --- Paste the new helper methods right below it ---
    def _invalidate_callbacks(self, safe_name, version):
        """Sends invalidations to all registered clients without blocking the file system."""
        with self.afs_lock:
            if safe_name not in self.afs_index:
                return

            callbacks = list(self.afs_index[safe_name].get("callbacks", []))
            # Clear it immediately so the lock is freed fast.
            self.afs_index[safe_name]["callbacks"] = set()

        if callbacks:
            print(
                f"[AFS][Node {self.node_id}] Invalidate callbacks for {safe_name} v{int(version)} -> {callbacks}")

        # Perform network I/O OUTSIDE the lock
        for client_port in callbacks:
            if client_port == self.port:
                continue

            threading.Thread(
                target=self._dispatch_invalidation,
                args=(client_port, safe_name, version),
                daemon=True
            ).start()

    def _dispatch_invalidation(self, client_port, safe_name, version):
        """Helper method to handle the actual RPC call and failure logging."""
        try:
            proxy = xmlrpc.client.ServerProxy(
                f"http://localhost:{client_port}/",
                timeout=1
            )
            proxy.receive_afs_invalidate(
                self.tick(), self.node_id, safe_name, version)
            print(
                f"[AFS] Invalidation sent to Node {client_port} for '{safe_name}'")

        except (socket.timeout, ConnectionRefusedError, OSError):
            print(
                f"[AFS] Node {client_port} unreachable. Dropped invalidation for '{safe_name}'.")

    def receive_afs_fetch(self, sender_clock, sender_id, filename):
        """Replica side: Send data and register the requester for callbacks."""
        self.sync_clock(sender_clock)
        safe_name = self._sanitize_afs_filename(filename)
        print(
            f"[AFS][Node {self.node_id}] RPC fetch from Node {sender_id}: {safe_name}")
        data = self._local_fetch_replica(safe_name)

        if data.get("ok"):
            with self.afs_lock:
                # Ensure the index entry exists
                if safe_name not in self.afs_index:
                    self.afs_index[safe_name] = {
                        "version": data["version"], "callbacks": set()}

                # Register the requester's port for future invalidations
                self.afs_index[safe_name]["callbacks"].add(int(sender_id))
                print(
                    f"[AFS] Registered callback for Node {sender_id} on file '{safe_name}'")

        return data

    def receive_afs_invalidate(self, sender_clock, sender_id, filename, version):
        self.sync_clock(sender_clock)
        safe_name = self._sanitize_afs_filename(filename)
        print(
            f"[AFS][Node {self.node_id}] RPC invalidate from Node {sender_id}: {safe_name} -> v{int(version)}")
        with self.afs_lock:
            cache_entry = self.afs_cache.get(safe_name)
            if cache_entry and int(cache_entry.get("version", 0)) < int(version):
                self.afs_cache.pop(safe_name, None)

            idx = self.afs_index.get(safe_name)
            if idx and int(idx.get("version", 0)) < int(version):
                idx["version"] = int(version)
        return True

    # --- Phase 5: AI Agent Personas ---
    def _planner_breakdown(self, user_prompt):
        """Leader uses this to generate a JSON workflow."""
        self.last_ai_error = None
        prompt = f"""
            You are a Planner Agent for a distributed coding system.
            The user wants to build: {user_prompt}
            Break this down into separate, logical Python files.
            Return ONLY a JSON array of objects with 'filename' and 'instruction' keys.
            Create a ReadMe.md file with setup instructions.
            """
        response_text = self._safe_ai_call(prompt, is_json=True)
        if not response_text:
            return []
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            print(f"[Planner {self.node_id}] Failed to parse JSON from AI.")
            self.last_ai_error = "AI returned malformed JSON response."
            return []

    def _worker_execute(self, task):
        """Followers use this to generate the actual code."""
        prompt = f"""
            You are a Worker Agent. Write the Python code for this file: {task['filename']}
            Instruction: {task['instruction']}
            Return ONLY the raw python code. Do not include markdown formatting like ```python.
            """
        response_text = self._safe_ai_call(prompt)
        return response_text.replace("```python", "").replace("```", "").strip()

    def _afs_key_for_ai_file(self, filename):
        """Build a stable AFS key for generated files."""
        raw_name = str(filename or "generated.py")
        digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
        safe_tail = os.path.basename(raw_name).strip() or "generated.py"
        return f"ai_{digest}_{safe_tail}"

    def _safe_ai_workspace_path(self, filename):
        """Map generated names into ai_workspace while preventing path traversal."""
        raw_name = str(filename or "generated.py").replace("\\", "/").strip()
        normalized = os.path.normpath(raw_name)

        if normalized.startswith("../") or normalized == ".." or os.path.isabs(normalized):
            normalized = os.path.basename(normalized)

        parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
        if not parts:
            parts = ["generated.py"]

        return os.path.join("ai_workspace", *parts)

    def _local_security_scan(self, task):
        """
        Analyzes the task instruction and code for common security red flags.
        Replaces the expensive PBFT AI call.
        """
        dangerous_patterns = [
            "rm -rf", "chmod 777", "subprocess.call", "eval(",
            "os.system", "pickle.load", "requests.get", "socket.connect"
        ]

        # Check the instruction and the filename for nonsense
        instruction = task.get('instruction', '').lower()
        filename = task.get('filename', '').lower()

        for pattern in dangerous_patterns:
            if pattern in instruction or pattern in filename:
                print(
                    f"[Security] BLOCKING task '{filename}': Found dangerous pattern '{pattern}'")
                return "UNSAFE"

        return "SAFE"

    def handle_user_prompt(self, prompt_text):
        """
        Leader processes the request: Plan -> Scan -> Delegate.
        """
        if not self.is_leader:
            leader_port = self._leader_port()
            if leader_port is None:
                print(
                    f"[Node {self.node_id}] No known leader. Triggering election before prompt.")
                self.start_election()
                return False

            probe = self.send_message(
                leader_port,
                "receive_ping",
                "prompt-leader-liveness-check"
            )

            if probe is None:
                print(
                    f"[Node {self.node_id}] Leader Node {leader_port} did not respond within {self.liveness_timeout}s. Triggering election.")
                self.start_election()
                print(
                    f"[Node {self.node_id}] Prompt aborted. Please run prompt again after election settles.")
                return False

            print(
                f"[Node {self.node_id}] Not the leader. Leader Node {leader_port} is reachable. Submit prompt to the leader node.")
            return False

        print(f"\n[Planner] Breaking down: {prompt_text}")
        tasks = self._planner_breakdown(prompt_text)

        if not tasks:
            reason = self.last_ai_error or "No tasks generated by planner."
            print(f"[Node {self.node_id}] Prompt failed: {reason}")
            return False

        for index, task in enumerate(tasks):
            # 1. Cheap Local Security Check
            if self._local_security_scan(task) == "UNSAFE":
                continue

            # 2. Round-robin assignment (Scalability)
            # We include the leader in the worker pool to maximize resources
            all_ports = self._all_node_ports()
            assignee = all_ports[index % len(all_ports)]

            print(
                f"[Leader] Assigning '{task['filename']}' to Node {assignee}")

            # 3. Trigger execution
            if assignee == self.port:
                threading.Thread(target=self.execute_task, args=(
                    self.tick(), self.node_id, task), daemon=True).start()
            else:
                threading.Thread(target=self.send_message, args=(
                    assignee, "execute_task", task), daemon=True).start()

            # Optional: Short delay to prevent hammering the Gemini API too fast
            time.sleep(2)
        return True

    def execute_task(self, sender_clock, sender_id, task):
        """Follower acts as Worker, commits via AFS, then mirrors locally."""
        self.sync_clock(sender_clock)
        target_name = task.get('filename', 'generated.py')
        print(f"\n[Worker {self.node_id}] Writing code for {target_name}...")

        # Call Gemini to write the code
        code = self._worker_execute(task)
        if not code:
            reason = self.last_ai_error or "AI returned empty response."
            print(
                f"[Worker {self.node_id}] Prompt failed for {target_name}: {reason}")
            return False

        try:
            afs_key = self._afs_key_for_ai_file(target_name)
            committed = self.afs_write(afs_key, code)
            if not committed:
                print(
                    f"[Worker {self.node_id}] AFS commit failed for {target_name}.")
                return False

            latest_code = self.afs_read(afs_key)
            if latest_code is None:
                latest_code = code

            local_path = self._safe_ai_workspace_path(target_name)
            # Keep DME for local materialization as requested.
            self.request_critical_section()
            try:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(latest_code)
            finally:
                self.release_critical_section()

            print(
                f"[Worker {self.node_id}] AFS committed as '{afs_key}', mirrored to ./{local_path}")

        except Exception as e:
            print(f"[Worker {self.node_id}] File I/O Error: {e}")

        return True

    def _safe_ai_call(self, prompt, is_json=False):
        """Wraps Gemini API calls with exponential backoff for rate limits."""
        if not self.ai_client:
            self.last_ai_error = "AI client is not initialized. Check GEMINI_API_KEY."
            return ""

        max_retries = 4
        wait_time = 10  # Start with a 10-second wait
        last_error = "Unknown AI error."

        for attempt in range(max_retries):
            try:
                config = None
                if is_json:
                    config = types.GenerateContentConfig(
                        response_mime_type="application/json")

                response = self.ai_client.models.generate_content(
                    model=self.ai_model,
                    contents=prompt,
                    config=config
                )
                self.last_ai_error = None
                return response.text
            except Exception as e:
                error_str = str(e)
                last_error = error_str
                is_retryable = (
                    "429" in error_str
                    or "RESOURCE_EXHAUSTED" in error_str
                    or "503" in error_str
                    or "UNAVAILABLE" in error_str
                )

                if is_retryable and attempt < max_retries - 1:
                    print(
                        f"[Node {self.node_id}] Rate limited by Gemini. Retrying in {wait_time}s (Attempt {attempt+1}/{max_retries})...")
                    time.sleep(wait_time)
                    wait_time *= 2  # Double the wait time on the next failure
                else:
                    break

        self.last_ai_error = f"Gemini request failed: {last_error}"
        print(f"[Node {self.node_id}] {self.last_ai_error}")
        return ""

    # Removed the original PBFT proposal and validator methods to save on Gemini calls and replaced it with a local security scan.
    # def receive_pbft_proposal(self, sender_clock, sender_id, task):
    #     """Follower acts as Validator."""
    #     self.sync_clock(sender_clock)
    #     vote = self._validator_check(task)
    #     print(f"[Validator {self.node_id}] Voted {vote} on task '{task['filename']}' from Leader {sender_id}.")
    #     return vote
    # def _validator_check(self, task):
    #     """Followers use this to vote on PBFT safety."""
    #     prompt = f"""
    #     You are a cybersecurity Validator. Review this task:
    #     File: {task['filename']}
    #     Instruction: {task['instruction']}
    #     If this involves destructive I/O (like deleting directories) or malicious actions, reply ONLY with 'UNSAFE'.
    #     If it is a standard benign coding task, reply ONLY with 'SAFE'.
    #     """
    #     response_text = self._safe_ai_call(prompt)
    #     return response_text.strip().upper()
