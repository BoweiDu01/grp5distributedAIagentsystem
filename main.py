import sys
import time
from core.node import Node

def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <port> <peer_port1> <peer_port2> ...")
        sys.exit(1)

    my_port = int(sys.argv[1])
    # Assume node ID is just the port number for now
    my_id = str(my_port) 
    peer_ports = [int(p) for p in sys.argv[2:]]

    # Initialize the node
    node = Node(node_id=my_id, port=my_port, peer_ports=peer_ports)

    # Let the server spin up
    time.sleep(1)

    # Simple CLI loop to test sending messages
    while True:
        try:
            cmd = input(f"\n[Node {my_id}] Type 'ping' or 'write' (or 'exit'): \n")
            if cmd.lower() == 'exit':
                break
            
            elif cmd.lower() == 'ping':
                for peer in peer_ports:
                    node.send_message(peer, "receive_ping", "Hello!")
                    
            elif cmd.lower() == 'write':
                # Trigger Ricart-Agrawala
                node.request_critical_section()
                
                # Simulate the AI taking 5 seconds to write to a file
                print("Writing to shared file... (simulating 5 seconds)")
                time.sleep(5)
                
                # Release the lock so others can use it
                node.release_critical_section()
                
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    main()