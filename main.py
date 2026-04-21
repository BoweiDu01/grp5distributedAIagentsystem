import sys
import time
from core.node import Node

LIVENESS_TIMEOUT_SECONDS = 20


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <port> <peer_port1> <peer_port2> ...")
        sys.exit(1)

    my_port = int(sys.argv[1])
    # Assume node ID is just the port number for now
    my_id = str(my_port)
    peer_ports = [int(p) for p in sys.argv[2:]]

    # Initialize the node
    node = Node(
        node_id=my_id,
        port=my_port,
        peer_ports=peer_ports,
        liveness_timeout=LIVENESS_TIMEOUT_SECONDS,
    )

    # Let the server spin up
    time.sleep(1)

    # Simple CLI loop to test commands
    while True:
        try:
            cmd = input(
                f"\n[Node {my_id}] Commands: ping | write | afs_write <file> <text> | afs_read <file> | afs_status | prompt <text> | exit\n"
            )
            if cmd.lower() == 'exit':
                break

            elif cmd.lower() == 'ping':
                if not node.is_leader:
                    leader_port = node._leader_port()
                    if leader_port is None:
                        print(
                            f"[Node {my_id}] No known leader during ping. Triggering election.")
                        node.start_election()
                    else:
                        probe = node.send_message(
                            leader_port,
                            "receive_ping",
                            "ping-leader-liveness-check"
                        )
                        if probe is None:
                            print(
                                f"[Node {my_id}] Leader Node {leader_port} is down. Triggering election.")
                            node.start_election()

                for peer in peer_ports:
                    node.send_message(peer, "receive_ping", "Hello!")

            elif cmd.lower() == 'write':
                # Trigger Ricart-Agrawala
                node.request_critical_section()
                print("Writing to shared file... (simulating 5 seconds)")
                time.sleep(5)
                node.release_critical_section()

            elif cmd.lower().startswith('afs_write '):
                parts = cmd.split(' ', 2)
                if len(parts) < 3:
                    print("Usage: afs_write <filename> <content>")
                else:
                    node.afs_write(parts[1], parts[2])

            elif cmd.lower().startswith('afs_read '):
                parts = cmd.split(' ', 1)
                if len(parts) < 2 or not parts[1].strip():
                    print("Usage: afs_read <filename>")
                else:
                    content = node.afs_read(parts[1].strip())
                    if content is not None:
                        print(f"AFS Content:\n{content}")

            elif cmd.lower() == 'afs_status':
                node.afs_status()

            # --- PHASE 5 AI INTEGRATION ---
            elif cmd.lower().startswith('prompt '):
                user_prompt = cmd[7:]  # Extract the text after "prompt "
                node.handle_user_prompt(user_prompt)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[Node {my_id}] Command failed without exiting: {e}")
            continue


if __name__ == "__main__":
    main()
