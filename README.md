# Fault-Tolerant Distributed AI Agent System

This project is a decentralized framework built from scratch in Python. It simulates a distributed network of AI agents capable of leader election, logical time synchronization, decentralized mutual exclusion, and collaborative AI-powered code generation without relying on external message brokers or centralized databases.

## Current Capabilities
* **Phase 1: P2P RPC Networking** (Custom XML-RPC implementation)
* **Phase 2: Logical Time** (Lamport Clocks)
* **Phase 3: Leader Election** (Bully Algorithm)
* **Phase 4: Distributed Mutual Exclusion** (Ricart-Agrawala Algorithm)
* **Phase 5: AI-Powered Code Generation** (PBFT Consensus with Gemini AI)

## Prerequisites
* Python 3.8+
* Google Gemini API key (for Phase 5 AI features)

## Setup
1. Install dependencies: `pip install google-genai`
2. Set up your Gemini API key as an environment variable or in your code.

## How to Run the Cluster

To simulate the distributed network locally, you must spin up multiple nodes in separate terminal windows. The first argument is the node's own port, followed by the ports of its peers.

1. **Terminal 1:** `python main.py 5001 5002 5003`
2. **Terminal 2:** `python main.py 5002 5001 5003`
3. **Terminal 3:** `python main.py 5003 5001 5002`

## Testing the Distributed Primitives

### 1. Test Network & Lamport Clocks
* Go to any terminal and type `ping`.
* **Expected Result:** The node will multicast a message to its peers. You will see the receiving nodes update their Lamport logical clocks based on the sender's timestamp.

### 2. Test Leader Election (Bully Algorithm)
* Upon startup, the nodes will wait ~5 seconds. Because no leader exists, they will automatically trigger an election.
* Node 5003 (the highest ID) will announce `*** [Node 5003] I am the new LEADER! ***`.
* **To test recovery:** Go to Terminal 3 (Node 5003) and terminate the process (`Ctrl + C`).
* **Expected Result:** After exactly 6 seconds of missing heartbeats, Nodes 5001 and 5002 will trigger a new election. Node 5002 will take over as the new Leader.

### 3. Test Distributed Mutual Exclusion (Ricart-Agrawala)
* Go to Terminal 1 and type `write`. It will request the Critical Section (CS), enter it, and simulate a 5-second file write.
* **To test collision/deferral:** Type `write` in Terminal 1, and *immediately* switch to Terminal 2 and type `write`.
* **Expected Result:** Terminal 1 will enter the CS. Terminal 2 will request it, but Terminal 1 will log `Deferring CS request from Node 5002`. Terminal 2 will wait until Terminal 1 finishes and releases the lock.

### 4. Test AI-Powered Code Generation (PBFT Consensus)
* Go to the Leader node's terminal (e.g., Node 5003) and type `prompt <description>`, e.g., `prompt create a simple tic-tac-toe game`.
* **Expected Result:** The Leader (Planner) breaks down the task into files. Followers (Validators) vote on safety using PBFT consensus. If approved, Workers generate code via Gemini AI and write files to the `ai_workspace/` directory using distributed mutual exclusion to prevent conflicts.