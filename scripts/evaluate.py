"""
Eval harness. Runs labeled cases through the real agent (needs ANTHROPIC_API_KEY) and
checks real OUTCOMES in the marketplace state: refund ledger rows, escalations, applied
substitutions, imported products. It does not trust the model's words.
State is reset before every case and uses its own file, so your dev data is untouched.

    python scripts/evaluate.py                    # all cases
    python scripts/evaluate.py --only refund      # ids containing 'refund'
    python scripts/evaluate.py --output results.json
Exit code is non-zero if any case fails (CI friendly).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cartpilot.agents.router import CartPilot  # noqa: E402
from cartpilot.marketplace.store import Store  # noqa: E402


def check(expect: dict, result: dict, store: Store, trace: list[dict] | None = None) -> list[str]:
    fails: list[str] = []
    trace = result.get("trace", []) if trace is None else trace   # multi-turn cases pass the combined trace
    called = [s["tool"] for s in trace]
    answer = result.get("answer") or ""

    def need(cond: bool, msg: str):
        if not cond:
            fails.append(msg)

    if "allowed" in expect:
        need(result["allowed"] == expect["allowed"], f"allowed={result['allowed']}, expected {expect['allowed']}")
    if "intent" in expect:
        need(result.get("intent") == expect["intent"], f"intent={result.get('intent')}, expected {expect['intent']}")
    for t in expect.get("tools_called", []):
        need(t in called, f"tool '{t}' was not called (called: {called})")
    if "needs_human_review" in expect:
        need(result.get("needs_human_review", False) == expect["needs_human_review"],
             f"needs_human_review={result.get('needs_human_review')} reasons={result.get('review_reasons')}")
    # --- outcomes in real state ---
    if "refunds" in expect:
        need(len(store.refunds()) == expect["refunds"], f"refund rows={len(store.refunds())}, expected {expect['refunds']}")
    if "refund_amount" in expect and store.refunds():
        need(store.refunds()[0]["amount"] == expect["refund_amount"], f"refund amount={store.refunds()[0]['amount']}")
    if "escalations_min" in expect:
        need(len(store.escalations()) >= expect["escalations_min"], f"escalations={len(store.escalations())}")
    if "substitutions" in expect:
        need(len(store.substitutions()) == expect["substitutions"], f"substitutions={len(store.substitutions())}")
    if "substitution_to_in" in expect and store.substitutions():
        need(store.substitutions()[0]["to"] in expect["substitution_to_in"], f"substituted to {store.substitutions()[0]['to']}")
    for pid in expect.get("product_exists", []):
        need(store.get_product(pid) is not None, f"product {pid} missing after import")
    for pid in expect.get("product_absent", []):
        need(store.get_product(pid) is None, f"product {pid} should not have been imported")
    for pid in expect.get("candidates_exclude", []):
        for step in trace:
            if step["tool"] == "suggest_substitutions" and isinstance(step.get("result"), dict):
                offered = {c["product_id"] for c in step["result"].get("candidates", [])}
                need(pid not in offered, f"{pid} was offered as a candidate")
    if "audit_actions_include" in expect:
        actions = {a["action"] for a in store.audit_entries()}
        for a in expect["audit_actions_include"]:
            need(a in actions, f"audit log has no '{a}' entry (has: {sorted(actions)})")
    for bad in expect.get("answer_not_contains", []):
        need(bad.lower() not in answer.lower(), f"answer leaked '{bad}'")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run cases whose id contains this text")
    ap.add_argument("--output", help="write JSON results here")
    args = ap.parse_args()

    cases = json.loads((ROOT / "data" / "eval" / "cases.json").read_text())
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]

    store = Store(state_path=ROOT / "data" / "eval_state.db")
    agent = CartPilot(store=store)
    results, failed = [], 0
    for case in cases:
        store.reset()
        try:
            prior = case.get("setup", {}).get("prior_refunds")
            for n in range(prior["count"] if prior else 0):  # seed history the policy engine should react to
                store.add_refund(order_id=f"hist{n}", item_id="i1", customer_id=prior["customer"], amount=5.0,
                                 reason="missing_item", approved_by="seed")
            result, trace = {}, []
            for turn in case.get("turns") or [case["query"]]:  # multi-turn cases share state, not chat history
                result = agent.handle(turn, case["actor_type"], case["actor_id"])
                trace += result.get("trace", [])
            fails = check(case["expect"], result, store, trace)
        except Exception as exc:  # a crash is a failure, not a pass
            result, fails = {}, [f"exception: {exc!r}"]
        ok = not fails
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {case['id']}")
        for f in fails:
            print(f"        - {f}")
        results.append({"id": case["id"], "pass": ok, "failures": fails, "answer": result.get("answer"),
                        "trace": result.get("trace"), "usage": result.get("usage")})
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
