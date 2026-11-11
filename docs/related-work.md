# Related work

contraprova builds on, and differs from, the following. Claims about other projects reflect their
public sources as of November 2026.

## Event-sourced agents

- **ESAA** — Elzo Brito dos Santos Filho, *ESAA: Event Sourcing for Autonomous Agents in LLM-Based
  Software Engineering*, arXiv [2602.23193](https://arxiv.org/abs/2602.23193) (2026);
  code: [elzobrito/ESAA-Core](https://github.com/elzobrito/ESAA-Core) (MIT). Agents emit validated
  intentions; a single-writer orchestrator appends them to an append-only log; state is a projection
  verified by replay hash. ESAA also has workflow gates, sha256-pinned edits, write boundaries,
  sandboxed executable checks, attempt limits, an optional hash chain and optional signed identities.
  contraprova adopts its central separation of proposal and validation. ESAA is the more complete
  governance system; contraprova's contribution is measurement (escalation value, cost, routing), not
  governance breadth. ESAA deliberately avoids MCP; contraprova uses MCP only as an optional edge façade.
- **ActiveGraph**, *The Log is the Agent*, arXiv [2605.21997](https://arxiv.org/abs/2605.21997) (2026):
  the log as the agent's primary store, with replay and forking.

## Replay and counterfactuals

- *The Replay Gap*, arXiv [2608.08239](https://arxiv.org/abs/2608.08239) (2026): swapping the model in a
  forked trajectory changes most later actions, so counterfactuals must be re-executed, not stitched.
  contraprova's forks follow this.
- *Chronicle*, arXiv [2609.20625](https://arxiv.org/abs/2609.20625) (2026): serving recorded boundaries
  from a log while running the rest live.
- Durable execution engines (Temporal, Restate, DBOS, LangGraph checkpoints) replay for recovery; none
  offers a control-corrected model counterfactual as a feature.

## Routing, cascades and cost

- FrugalGPT (Chen et al., 2023); RouteLLM (Ong et al., ICLR 2025); AutoMix; bandit routers such as
  ParetoBandit and OrcaRouter (2026). These compare a cheap model with an expensive one, but not with a
  same-model retry from the same point.
- Anthropic, *How we built our multi-agent research system* (June 2025), and the advisor tool (April 2026).
- Kapoor et al., *AI Agents That Matter* (2024), and HAL (ICLR 2026): report cost and accuracy together,
  on a Pareto frontier.
- Google, *Towards a Science of Scaling Agent Systems*, arXiv [2512.08296](https://arxiv.org/abs/2512.08296):
  multi-agent setups help decomposable work and hurt sequential work.

## Verification and failure analysis

- MAST failure taxonomy (Cemri et al., arXiv [2503.13657](https://arxiv.org/abs/2503.13657)).
- Who&When failure attribution, arXiv [2505.00212](https://arxiv.org/abs/2505.00212).
- ImpossibleBench (ICLR 2026): coding agents modify tests to pass them; read-only tests help.
- Panickssery et al. (NeurIPS 2024) and Goel et al. (ICML 2025): judges prefer their own and similar
  models' outputs, so same-family review is not independent.

## Parallel lanes and shared context

- Skeleton-of-Thought, arXiv [2307.15337](https://arxiv.org/abs/2307.15337): plan, then expand sections
  in parallel.
- TensorFold ([ashhart/TensorFold](https://github.com/ashhart/TensorFold), Apache-2.0): shared lane
  rounds with per-stream speculative decoding; its public demo fanned one request out into eight lanes.
  Contract Lanes borrows the pattern, not the code.
- Hogwild! Inference (Rodionov et al., NeurIPS 2025, arXiv [2504.06261](https://arxiv.org/abs/2504.06261)):
  parallel workers over one shared, concurrently updated attention cache.
