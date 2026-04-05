import threading
from xmlrpc.server import SimpleXMLRPCServer
import xmlrpc.client
import time

class Node:
        def __init__(self, node_id, port, peer_ports):
            self.node_id = node_id
            self.port = port
            self.peer_ports = peer_ports  
            
            # Phase 2: Lamport Logical Clock
            self.logical_clock = 0
            self.clock_lock = threading.Lock() 
            
            # Phase 3: Leader Election State
            self.leader_id = None
            self.is_leader = False
            self.last_heartbeat = time.time()
            self.election_in_progress = False

            # Start the RPC Server
            self.server_thread = threading.Thread(target=self._start_server, daemon=True)
            self.server_thread.start()

            # Phase 3: Start Heartbeat & Monitor Threads
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()
            threading.Thread(target=self._monitor_leader, daemon=True).start()

            # Phase 4: Ricart-Agrawala DME State
            self.cs_state = 'RELEASED'  
            self.cs_request_timestamp = 0
            self.cs_replies_received = 0
            self.deferred_requests = []
            self.cs_lock = threading.Lock() # Protects DME state from race conditions

        def _start_server(self):
            """Phase 1: Initialize the XML-RPC server to listen for incoming messages."""
            # logRequests=False keeps the terminal from getting too messy
            server = SimpleXMLRPCServer(("localhost", self.port), logRequests=False, allow_none=True)
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

        def send_message(self, target_port, method_name, *args):
            """Helper to send RPC calls to other nodes, attaching the current clock."""
            current_time = self.tick() # Tick before sending
            target_url = f"http://localhost:{target_port}"
            
            try:
                with xmlrpc.client.ServerProxy(target_url) as proxy:
                    method = getattr(proxy, method_name)
                    # We always pass our Lamport clock as the first argument
                    return method(current_time, self.node_id, *args)
            except ConnectionRefusedError:
                print(f"[Node {self.node_id}] Failed to connect to port {target_port}.")
                return None

        # --- RPC Exposed Methods (Callable by other nodes) ---

        def receive_ping(self, sender_clock, sender_id, message):
            """A simple method to test connectivity and clock syncing."""
            updated_time = self.sync_clock(sender_clock)
            print(f"\n[Node {self.node_id}] Received Ping from Node {sender_id}: '{message}'")
            print(f"[Node {self.node_id}] Clock Synced: Sender({sender_clock}) -> Local({updated_time})")
            return True
        
        # --- Phase 3: Heartbeat & Monitoring ---

        def _heartbeat_loop(self):
            """Leader exclusively runs this to let others know it is alive."""
            while True:
                if self.is_leader:
                    for peer in self.peer_ports:
                        self.send_message(peer, "receive_heartbeat")
                time.sleep(2) # Send a heartbeat every 2 seconds

        def _monitor_leader(self):
            """Followers run this to detect if the leader has crashed."""
            time.sleep(5) # Give the cluster 5 seconds to boot up initially
            while True:
                if not self.is_leader and not self.election_in_progress:
                    # If 6 seconds pass without a heartbeat, assume leader is dead
                    if time.time() - self.last_heartbeat > 6:
                        print(f"\n[Node {self.node_id}] Leader timeout! Initiating election.")
                        self.start_election()
                time.sleep(1)
        # --- Phase 3: Bully Algorithm Logic ---

        def start_election(self):
            self.election_in_progress = True
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
                if reply: # An active higher node responded 'OK'
                    got_ok = True

            if not got_ok:
                # Higher nodes exist, but none responded. They must be dead. I win.
                self.become_leader()
            else:
                # A higher node responded. Step down and wait for them to take over.
                self.election_in_progress = False

        def become_leader(self):
            print(f"\n*** [Node {self.node_id}] I am the new LEADER! ***")
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
            self.last_heartbeat = time.time()
            
            # If a new node joins and starts sending heartbeats, accept it
            if self.leader_id != sender_id:
                print(f"[Node {self.node_id}] Recognizing new leader: Node {sender_id}")
                self.leader_id = sender_id
                
            self.is_leader = False
            self.election_in_progress = False
            return True

        def receive_election(self, sender_clock, sender_id):
            """Called by a lower-ID node starting an election."""
            self.sync_clock(sender_clock)
            print(f"[Node {self.node_id}] Received ELECTION from {sender_id}. Replying OK.")
            
            # If we aren't already running an election, start one to challenge higher nodes
            if not self.election_in_progress:
                threading.Thread(target=self.start_election, daemon=True).start()
            return True # Returning True acts as the "OK" message

        def receive_coordinator(self, sender_clock, sender_id):
            """Called by the new Leader declaring victory."""
            self.sync_clock(sender_clock)
            print(f"\n[Node {self.node_id}] Node {sender_id} has declared itself COORDINATOR.")
            self.leader_id = sender_id
            self.is_leader = False
            self.election_in_progress = False
            self.last_heartbeat = time.time()
            return True 
        # --- Phase 4: Ricart-Agrawala Core Logic ---

        def request_critical_section(self):
            """Called locally when the node wants to write to the shared resource."""
            with self.cs_lock:
                self.cs_state = 'WANTED'
                self.cs_request_timestamp = self.tick()
                self.cs_replies_received = 0
                self.deferred_requests.clear()
            
            print(f"\n[Node {self.node_id}] Requesting Critical Section at TS {self.cs_request_timestamp}")
            
            # Broadcast request to all peers
            for peer in self.peer_ports:
                # Send in a background thread so we don't block
                threading.Thread(
                    target=self.send_message, 
                    args=(peer, "receive_cs_request", self.cs_request_timestamp), 
                    daemon=True
                ).start()
                
            # Wait until we get replies from EVERY peer
            while True:
                with self.cs_lock:
                    if self.cs_replies_received >= len(self.peer_ports):
                        self.cs_state = 'HELD'
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
                    print(f"[Node {self.node_id}] Sending deferred reply to Node {deferred_id}")
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
                    print(f"[Node {self.node_id}] Deferring CS request from Node {sender_id}")
                    self.deferred_requests.append(sender_id)
                else:
                    # We don't care, or they have priority. REPLY immediately.
                    peer_port = int(sender_id)
                    threading.Thread(
                        target=self.send_message, 
                        args=(peer_port, "receive_cs_reply"), 
                        daemon=True
                    ).start()
            return True

        def receive_cs_reply(self, sender_clock, sender_id):
            """Handles an incoming reply granting permission."""
            self.sync_clock(sender_clock)
            with self.cs_lock:
                self.cs_replies_received += 1
                print(f"[Node {self.node_id}] Received CS permission from Node {sender_id} ({self.cs_replies_received}/{len(self.peer_ports)})")
            return True