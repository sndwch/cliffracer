# Architecture Notes: `cliffracer-actors`

*Status: Draft / Brainstorming*

The Virtual Actor model (inspired by Microsoft Orleans, Akka, and Cloudflare Durable Objects) is the ideal next step for Cliffracer. By leaning into our "broker-native" philosophy, we can bypass years of distributed systems complexity by offloading the hardest problems to NATS. 

However, we must strictly address the following distributed systems traps before writing any code.

---

## 1. Directory and Placement (Subject-per-Actor)
**The Trap:** Building a custom membership protocol or distributed hash table (DHT) to track which node owns which actor is incredibly complex and fragile.
**The NATS-Native Solution:** 
- Map the actor's address directly to a NATS subject: `actors.shoppingcart.user_123`.
- An active actor is simply an **exclusive subscription** to that subject by the owning node.
- **Routing:** Callers blindly publish to the subject. NATS interest-based routing delivers it to the owner.
- **Activation:** If a node dies, the subscription drops. The next caller gets a NATS `no-responders` error. This error becomes the **activation trigger**: a node catches the error, claims the subject, and hydrates the actor from KV.

## 2. Split Brain and Fencing (KV CAS)
**The Trap:** A network partition occurs. Node A is partitioned but not dead. Node B gets `no-responders` and activates a second instance. Both nodes believe they own `ShoppingCart(user_123)` and both flush state, silently corrupting the database (last-write-wins).
**The NATS-Native Solution:**
- Every KV write must use **Fencing**.
- Each activation tracks the NATS KV `revision` number of the state it hydrated.
- Every state flush must be a **Compare-and-Swap (CAS)** operation against the expected revision (which NATS KV natively supports).
- If Node A tries to flush but the revision has changed (because Node B activated and wrote), the flush **fails loudly**, and Node A immediately kills its stale activation.

## 3. Durability Semantics (No Silent Write-Behind)
**The Trap:** Flushing state in the background (write-behind) provides in-memory speed but loses data if the node crashes before the flush.
**The Solution:**
In accordance with Cliffracer's philosophy ("when multiple interpretations have materially different operational consequences, require the developer to choose"), durability must be explicitly configured:
- `durability="write-through"`: The RPC does not return `ACK` to the caller until NATS KV confirms the CAS write. (Database safety, slower).
- `durability="write-behind"`: The RPC returns immediately; state is flushed on an interval. (In-memory speed, potential data loss).

## 4. Concurrency and Mailboxes
**The Trap:** If an actor processes multiple RPC requests concurrently (using `asyncio.gather` style execution), it is just shared mutable state with race conditions. 
**The Solution:**
- Actors must have a **single-threaded mailbox**. Messages are processed strictly one at a time per actor instance.
- **Deadlock Detection:** If Actor A calls Actor B, and Actor B calls Actor A, the single-threaded mailbox will deadlock. The framework must detect cyclical call chains (via distributed tracing headers) and raise a fast failure.

## 5. Passivation and Health
- **Eviction:** "Stays awake in memory" must be bounded by an LRU cache or idle-timeout (passivation) to prevent OOMs when serving millions of entities.
- **Health Probes:** The existing `@dependency` health machinery must monitor actor hosts. If a node's actor mailbox lag crosses a threshold or its activation count hits capacity, it should fail its readiness probe.

---
**Required Reading before Implementation:**
1. *Orleans: Distributed Virtual Actors for Programmability and Scalability* (Paper)
2. *Cloudflare Durable Objects: Input/Output Gates* (Blog Post)
