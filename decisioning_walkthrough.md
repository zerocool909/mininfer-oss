# Walkthrough: Usage-Based Compare Diversity & Routing RLHF

We have implemented two complementary enhancements to address model dominance and introduce routing decision feedback:
1. **UCB1 (Upper Confidence Bound) Exploration for Compare Mode Diversity** to rotate and surface under-tested free models alongside the top recommendation.
2. **Bayesian Conjugate Routing Decision RLHF** enabling users to give thumbs up / down directly on the model selection rationale in the chat details drawer.

---

## 1. UCB1 Multi-Armed Bandit Exploration in Compare Mode

### The Problem
When running in compare mode, the system deterministically selected candidates. Because `groq:qwen/qwen3.8-27b` accumulated a high observation count and high Wilson lower bound (`p_lb`), it always won Arm 0, and the same fixed runner-up won Arm 1. Other capable free deployments (SambaNova, Novita, Vercel, Mistral, Cerebras, OpenRouter free models) were starved of traffic.

### The Solution: UCB1 Exploration
In [`mininfer/router.py`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/mininfer/router.py) and [`mininfer/proxy.py`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/mininfer/proxy.py):
- **Arm 0 (Exploitation)**: Strictly preserved as the top deterministic model chosen by the policy objective (e.g. `groq:qwen/qwen3.8-27b`).
- **Arm 1 (Exploration)**: Ranked using UCB1:
  $$\text{Score}_{\text{compare}}(c) = \mu_c + c_{\text{explore}} \cdot \sqrt{\frac{\ln(1 + N_{\text{pool}})}{1 + n_c}}$$
  Under-tested models with low $n_c$ receive a mathematically principled exploration bonus.
- `_distinct_options()` then filters for distinct makers and upstreams, ensuring Arm 1 rotates across different providers and models across successive compare queries.

---

## 2. Model Selection RLHF (Routing Decision Feedback)

### The Problem
Existing user feedback (`/v1/approve`) only recorded whether the generated text answer was good or bad (`signal_kind='subjective'`). Users had no way to indicate whether the router picked the *right model* for the given task.

### The Solution
1. **Database & Store Layer**:
   - In [`mininfer/store.py`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/mininfer/store.py), added `routing_approval_stats(task)` to aggregate approvals and disapprovals where `signal_kind = 'routing_approval'`.
2. **Bayesian Conjugate Sentiment Update**:
   - In [`mininfer/router.py`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/mininfer/router.py), human feedback updates a $\text{Beta}(\alpha=2, \beta=2)$ prior:
     $$\text{sentiment} = \frac{2 + \text{approvals}}{4 + \text{approvals} + \text{disapprovals}}$$
   - Downvotes reduce the candidate's effective score in `_sort_key` and `build_candidates()`, allowing competing alternatives to overtake repetitive or ill-suited models.
3. **Backend API Endpoint**:
   - In [`mininfer/proxy.py`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/mininfer/proxy.py), added `POST /v1/route-verdict`:
     ```json
     {
       "deploy_id": "groq:qwen/qwen3.8-27b",
       "task": "general_chat",
       "approved": false,
       "reason": "overused model"
     }
     ```
4. **Interactive UI in Chat Details**:
   - In [`TelemetryHud.tsx`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/web/src/components/chat/TelemetryHud.tsx), added **"Good choice"** (Thumbs Up) and **"Wrong choice"** (Thumbs Down) buttons inside the expanded **Routing Decision Verdict** drawer.
   - In [`chat-02.tsx`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/web/src/components/ui/chat-02.tsx) and [`api.ts`](file:///Users/a80216319/Documents/zed/pfo/dynamic_router/web/src/lib/api.ts), wired up real-time feedback submissions and persisted turn state.

---

## 3. Verification & Testing

1. **Python Imports & Logic**:
   - Tested UCB1 ranking: Verified that Arm 0 retains the deterministic winner (`groq:qwen/qwen3.8-27b`), while Arm 1 selects under-tested models (`sambanova:meta/llama-3.3-70b` with $n=0$ observations) over models with higher observation counts.
   - Tested Store aggregation: Verified that `routing_approval_stats` correctly tallies approvals and disapprovals.
2. **FastAPI Route Verification**:
   - Tested `POST /v1/route-verdict` with `TestClient`: Verified 200 OK and persistent storage into SQLite `observations`.
3. **Web Production Build**:
   - Executed `npm --prefix web run build`: Successfully built Vite bundle with 0 TypeScript/ESLint errors.
