# Fault-Tolerant Distributed AI Agent System

This project is a decentralized framework built from scratch in Python. It simulates a distributed network of AI agents capable of leader election, logical time synchronization, decentralized mutual exclusion, and collaborative AI-powered code generation without relying on external message brokers or centralized databases.

## Current Capabilities
* **Phase 1: Logical Time** (Lamport Clocks)
* **Phase 2: Leader Election** (Bully Algorithm)
* **Phase 3: Distributed Mutual Exclusion** (Ricart-Agrawala Algorithm)
* **Phase 4: AFS-lite Fault-Tolerant File Storage** (Replicated files with quorum semantics)
* **Phase 5: AI-Powered Code Generation** (Integration with Gemini AI Code Generation)

## Prerequisites
* Python 3.8+
* Google Gemini API key (for Phase 5 AI features)

## Setup
1. Create a virtual environment to keep your workspace clean: `python -m venv .venv`
2. Activate the virtual environment: 
	* On Windows `venv\Scripts\activate` 
	* On Max/Linux `source venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Set up your Gemini API key as an environment variable or in your code. You can obtain one via the Google AI Studio https://ai.google.dev/gemini-api/docs/api-key

Follow these steps to create your key:
1. Go to Create or View a Gemini API Key.

2. Select Project (Left sidebar) -> Create a New Project.

3. Name your Project (e.g., grp1) -> Create Project.

4. Select API Keys (Left sidebar) -> Create API Key.

5. Name your key (e.g., grp1apikey).

6. Choose the imported project you just created (grp1) -> Create Key.

Create a .env file in the root directory of the project and copy your new API key into it exactly like this: 
> GEMINI_API_KEY=your_actual_api_key_here

If your API Key has been successfully set up, run the test script: `python test_gemini.py`

If successful, you will see 
> Sending Ping to Gemini...

> Response: API Connection Successful

## How to Run the Cluster

To simulate the distributed network locally, you must spin up multiple nodes in separate terminal windows. The first argument is the node's own port, followed by the ports of its peers.

1. **Terminal 1:** `python main.py 5001 5002 5003`
2. **Terminal 2:** `python main.py 5002 5001 5003`
3. **Terminal 3:** `python main.py 5003 5001 5002`

## Testing the Distributed Primitives

### 1. Test Network & Lamport Clocks
* Go to any terminal and type `ping`.
**Expected Result:** The node will multicast a message to its peers. You will see the receiving nodes update their Lamport logical clocks based on the sender's timestamp.

### 2. Test Leader Election (Bully Algorithm)
* Upon startup, the nodes will wait ~5 seconds. Because no leader exists, they will automatically trigger an election.
* Node 5003 (the highest ID) will announce `*** [Node 5003] I am the new LEADER! ***`.
* **To test recovery:** Go to Terminal 3 (Node 5003) and terminate the process (`Ctrl + C`).
**Expected Result:** After exactly 6 seconds of missing heartbeats, Nodes 5001 and 5002 will trigger a new election. Node 5002 will take over as the new Leader.

### 3. Test Distributed Mutual Exclusion (Ricart-Agrawala)
* Go to Terminal 1 and type `write`. It will request the Critical Section (CS), enter it, and simulate a 5-second file write.
* **To test collision/deferral:** Type `write` in Terminal 1, and *immediately* switch to Terminal 2 and type `write`.
**Expected Result:** Terminal 1 will enter the CS. Terminal 2 will request it, but Terminal 1 will log `Deferring CS request from Node 5002`. Terminal 2 will wait until Terminal 1 finishes and releases the lock.

### 4. Test AFS-lite Fault-Tolerant File Storage

The cluster now supports a lightweight Andrew File System style layer with replication and versioned reads/writes.

#### Available Commands
* `afs_write <filename> <content>`: Writes a new version of a file to replicated nodes.
* `afs_read <filename>`: Reads the latest version across replicas and performs best-effort repair of stale copies.
* `afs_status`: Shows local AFS index/cache state for the current node.

#### Basic Write/Read Test
1. Start the same 3-node cluster:
	* Terminal 1: `python main.py 5001 5002 5003`
	* Terminal 2: `python main.py 5002 5001 5003`
	* Terminal 3: `python main.py 5003 5001 5002`
2. In any terminal, write a file:
	* `afs_write notes.txt hello from node`
3. In another node terminal, read it:
	* `afs_read notes.txt`

**Expected Result:** You should see `AFS read: notes.txt v1` and the same content returned from a different node.

#### Versioning Test
1. Run a second write from any node:
	* `afs_write notes.txt updated content`
2. Read from all nodes using `afs_read notes.txt`.

**Expected Result:** All reads converge to the newest version (for example `v2`).

#### Failure Tolerance Test (Replica Node Down)
1. Write a file first:
	* `afs_write report.txt initial draft`
2. Stop one node (Ctrl + C in one terminal).
3. From a surviving node, run:
	* `afs_read report.txt`

**Expected Result:** Read still succeeds as long as quorum/replicas are still reachable.

#### Recovery/Repair Test
1. Restart the stopped node using its original command.
2. From a live node, run:
	* `afs_read report.txt`

**Expected Result:** During read, replicas are refreshed best-effort, and subsequent reads on the restarted node return the latest version.

#### Local Storage Layout
* Replicas are stored per node in `afs_storage/node_<port>/`.
* Each replicated object is saved as `<filename>.afs.json` with `version` and `content` fields.

### 5. Test AI-Powered Code Generation
* Go to the Leader node's terminal (e.g., Node 5003) and type `prompt <description>`, e.g., `prompt create a simple tic-tac-toe game`.

**Expected Result:** 
1. Planning: The Leader (Planner) uses Gemini to break down the user request into separate files and instructions.
2. Security Scan: The Leader performs a fast local scan to block destructive I/O commands (replacing the slower PBFT consensus).
3. Delegation: Approved tasks are delegated to worker nodes in a round-robin fashion.
4. Execution & AFS Commit: Workers call Gemini to write the code and commit it to the distributed AFS network using afs_write, storing it safely across node replicas.
5. Local Mirroring: After securing the code in AFS, the worker retrieves the latest version and writes a materialized copy to its local ai_workspace/ directory. It uses Distributed Mutual Exclusion (Ricart-Agrawala) during this step to prevent local I/O race conditions.
