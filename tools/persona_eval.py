"""Reproducible persona evaluation; no real corpus, index, or implicit network."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qq_chatbot.chat import Chatbot
from qq_chatbot.config import Config
from qq_chatbot.llm import make_llm
from qq_chatbot.profile import profile_cache_key
from qq_chatbot.schema import PersonaProfile, RetrievedTurn, Speaker, Turn
from qq_chatbot.session import Session

DIMENSIONS = ("贴题", "像本人", "自然长度", "事实归属", "连贯")
SCHEMA = "persona-eval-v1"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_suite(split="dev"):
    # Deliberately read only the selected split: dev never opens holdout.
    if split not in ("dev", "holdout"):
        raise ValueError("split must be dev or holdout")
    suite = read_json(Path(__file__).with_name(f"persona_eval_{split}.json"))
    cases = suite["cases"]
    if suite["split"] != split or len(cases) != (16 if split == "dev" else 8):
        raise ValueError("invalid suite split/count")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate case id")
    return suite


def output_dir(raw):
    path = Path(raw).resolve()
    # Only explicitly selected local evaluation directories in tools/ or tests/.
    if str(path).startswith("\\\\") or ".chatbot" in {p.lower() for p in path.parts}:
        raise ValueError("output must be local and outside .chatbot")
    if not any(path.is_relative_to(ROOT / name) and path != ROOT / name for name in ("tools", "tests")):
        raise ValueError("output must be a new evaluation directory below repository tools/ or tests/")
    if path.exists():
        raise ValueError("output directory already exists; choose a new directory")
    path.mkdir(parents=True)
    return path


def save(path, name, value):
    (path / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class FixedRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def retrieve(self, query, **filters):
        self.calls.append({"query": query, "filters": filters})
        return list(self.hits)


class RecordingLLM:
    def __init__(self, delegate=None):
        self.delegate = delegate
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "parameters": kwargs})
        if self.delegate is None:
            return "[offline-contract-mock] 此占位回复只用于检查编排，不代表人物表现。"
        return self.delegate.chat(messages, **kwargs)


def evaluate_case(suite, case, inject, delegate=None, temperature=0.4, max_tokens=1024):
    cfg = Config()  # Do not load config files or inherited production configuration.
    cfg.llm.temperature, cfg.llm.max_tokens = temperature, max_tokens
    cfg.retrieval.persona_inject_retrieval = inject
    cfg.retrieval.min_score = 0.35
    cfg.memory.enabled = cfg.observability.enabled = False
    profile = PersonaProfile(**suite["profile"])
    kind, conversation = "c2c", "c2c:fictional-evaluation"
    hits = [RetrievedTurn(Turn(
        id=f"{case['id']}-{i}", speaker=Speaker(profile.speaker, profile.name),
        conversation_key=conversation, kind=kind, time=1700000000 + i,
        time_str=f"2023-11-14 22:13:{20+i:02d}", text=item["text"]), item["score"])
        for i, item in enumerate(case["retrieved"])]
    retriever, llm = FixedRetriever(hits), RecordingLLM(delegate)
    bot = Chatbot(cfg, retriever, llm, corpus=None,
                  profiles={profile_cache_key(profile.speaker, kind, conversation): profile})
    session = Session()
    session.history = [dict(m) for m in case["history"]]
    answer = bot.answer(case["query"], mode="persona", person=profile.speaker,
                        kind=kind, conversation=conversation, session=session)
    good = [h for h in hits if h.score >= 0.35] if inject else []
    messages = llm.calls[0]["messages"] if len(llm.calls) == 1 else []
    last = messages[-1]["content"] if messages else ""
    expected_citations = [bot._cite(h).__dict__ for h in good[:5]]
    checks = {
        "one_generation": len(llm.calls) == 1,
        "query_and_history": bool(messages) and case["query"] in last and messages[1:-1] == case["history"],
        "cached_profile": bool(messages) and profile.name in messages[0]["content"] and all(f["fact"] in messages[0]["content"] for f in profile.facts),
        "injection_and_threshold": all((h.turn.text in last) == (h in good) for h in hits),
        "citations": [c.__dict__ for c in answer.citations] == expected_citations,
        "grounded": answer.grounded == bool(good),
        "retrieval_scope": len(retriever.calls) == 1 and retriever.calls[0]["filters"] == {"person_qq": profile.speaker.rsplit(":", 1)[-1], "conversation": conversation},
    }
    return {"case_id": case["id"], "policy": "on" if inject else "off", "text": answer.text,
            "checks": checks, "contract_passed": sum(checks.values()), "contract_total": len(checks),
            "grounded": answer.grounded, "citations": [c.__dict__ for c in answer.citations],
            "messages": messages, "human_scores": dict.fromkeys(DIMENSIONS)}


def run(args):
    if args.split == "all":
        if args.mode == "import":
            raise ValueError("import dev/holdout separately to keep reply sets independent")
        out = output_dir(args.output)
        statuses, reports = [], []
        for split in ("dev", "holdout"):
            child = argparse.Namespace(**vars(args))
            child.split, child.output = split, str(out / split)
            statuses.append(run(child))
            reports.append((out / split / "report.md").read_text(encoding="utf-8"))
        (out / "report.md").write_text("\n\n".join(reports), encoding="utf-8")
        print(out / "report.md")
        return max(statuses)
    suite = load_suite(args.split)
    delegate = None
    if args.mode == "llm":
        if not args.provider or not args.model or not args.base_url:
            raise ValueError("llm requires explicit --provider, --model and --base-url")
        cfg = Config().llm
        cfg.provider, cfg.model, cfg.base_url = args.provider, args.model, args.base_url
        cfg.api_key = os.environ.get(args.api_key_env, "")
        cfg.temperature, cfg.max_tokens = args.temperature, args.max_tokens
        delegate = make_llm(cfg)
    rows = []
    imported = read_json(args.replies) if args.mode == "import" else None
    if imported is not None:
        expected = {f"{c['id']}:{p}" for c in suite["cases"] for p in ("off", "on")}
        if set(imported) != expected or any(not isinstance(t, str) or not t.strip() for t in imported.values()):
            raise ValueError("reply JSON must contain exactly every case_id:off/on with nonempty text")
    for case in suite["cases"]:
        for inject in (False, True):
            row = evaluate_case(suite, case, inject, delegate, args.temperature, args.max_tokens)
            if imported is not None:
                row["text"] = imported[f"{row['case_id']}:{row['policy']}"]
            rows.append(row)
    result = {"schema": SCHEMA, "split": args.split, "suite_hash": digest(suite),
              "suite": suite, "mode": args.mode, "label": args.label,
              "generation": {"provider": args.provider, "model": args.model, "base_url": args.base_url,
                             "temperature": args.temperature, "max_tokens": args.max_tokens},
              "contract_source": "actual Chatbot orchestration with fixed inputs; imported text is not generation validation",
              "human_status": "UNSCORED; mock cannot establish human likeness", "rows": rows}
    out = output_dir(args.output)
    save(out, "run.json", result)
    report = contract_report(result)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(out / "report.md")
    return 0 if all(all(r["checks"].values()) for r in rows) else 1


def contract_report(result):
    lines = [f"Persona evaluation: {result['label']} ({result['split']}, {result['mode']})", "",
             f"Suite SHA256: {result['suite_hash']}", "",
             "Automatic scores measure orchestration contracts only. All five human dimensions: UNSCORED.",
             "Mock responses cannot establish naturalness or human likeness. No overall persona PASS is asserted.", "",
             "| Policy | Contract passed/total | Failed checks |", "|---|---:|---|"]
    for policy in ("off", "on"):
        rows = [r for r in result["rows"] if r["policy"] == policy]
        failures = [f"{r['case_id']}:{k}" for r in rows for k, ok in r["checks"].items() if not ok]
        lines.append(f"| {policy} | {sum(r['contract_passed'] for r in rows)}/{sum(r['contract_total'] for r in rows)} | {', '.join(failures) or 'none'} |")
    return "\n".join(lines) + "\n"


def blind(args):
    runs = [read_json(p) for p in args.runs]
    if any(r["schema"] != SCHEMA or r["suite_hash"] != runs[0]["suite_hash"] for r in runs):
        raise ValueError("runs must share schema, split and exact suite hash")
    cards, mapping = [], {}
    rng = random.Random(args.seed)
    candidates = [(i, row) for i, r in enumerate(runs) for row in r["rows"]]
    rng.shuffle(candidates)
    for n, (i, row) in enumerate(candidates):
        token = f"blind-{n+1:04d}"
        case = next(c for c in runs[i]["suite"]["cases"] if c["id"] == row["case_id"])
        cards.append({"blind_id": token, "case_id": row["case_id"], "profile": runs[i]["suite"]["profile"],
                      "query": case["query"], "history": case["history"],
                      "available_evidence": [h for h in case["retrieved"] if row["policy"] == "on" and h["score"] >= 0.35],
                      "rubric": case["rubric"], "reply": row["text"], "scores": dict.fromkeys(DIMENSIONS), "notes": ""})
        mapping[token] = {"run": i, "case_id": row["case_id"], "policy": row["policy"]}
    out = output_dir(args.output)
    save(out, "blind.json", {"schema": SCHEMA, "cards": cards})
    save(out, "private-key.json", {"schema": SCHEMA, "runs": runs, "mapping": mapping,
                                   "blind_hash": digest(cards)})
    print(out / "blind.json")
    return 0


def score(args):
    sheet, key = read_json(args.sheet), read_json(args.key)
    cards = sheet["cards"]
    mapping = key["mapping"]
    if len(cards) != len(mapping) or {c["blind_id"] for c in cards} != set(mapping):
        raise ValueError("missing, duplicate or unknown blind id")
    # Bind scores to the original blinded text and context, not just token names.
    originals = []
    for card in cards:
        original = dict(card)
        original["scores"], original["notes"] = dict.fromkeys(DIMENSIONS), ""
        originals.append(original)
    if digest(originals) != key["blind_hash"]:
        raise ValueError("blind content/order modified; edit scores and notes only")
    buckets = {}
    for card in cards:
        if set(card["scores"]) != set(DIMENSIONS):
            raise ValueError("scores must have exactly the five dimensions")
        for value in card["scores"].values():
            if value is not None and (type(value) is not int or value not in (0, 1, 2)):
                raise ValueError("score must be integer 0, 1, 2 or null")
        item = mapping[card["blind_id"]]
        buckets.setdefault((item["run"], item["policy"]), {})[item["case_id"]] = card["scores"]
    lines = ["Human persona evaluation", "", "0 = fails; 1 = mixed; 2 = meets expectation. Null = UNSCORED, never a pass.", "",
             "| Run / policy | Dimension | Mean / 2 | Scored / total |", "|---|---|---:|---:|"]
    summary = []
    for (i, policy), values in sorted(buckets.items()):
        for dim in DIMENSIONS:
            scored = [v[dim] for v in values.values() if v[dim] is not None]
            mean = sum(scored) / len(scored) if scored else None
            summary.append({"run": i, "label": key["runs"][i]["label"], "policy": policy, "dimension": dim,
                            "mean": mean, "scored": len(scored), "total": len(values)})
            lines.append(f"| {i}:{key['runs'][i]['label']} / {policy} | {dim} | {mean if mean is not None else 'UNSCORED'} | {len(scored)}/{len(values)} |")
    lines += ["", "Paired differences (candidate minus run 0/off, only jointly scored cases):", ""]
    baseline = buckets.get((0, "off"), {})
    for group, values in sorted(buckets.items()):
        if group == (0, "off"):
            continue
        for dim in DIMENSIONS:
            deltas = [v[dim] - baseline[c][dim] for c, v in values.items()
                      if c in baseline and v[dim] is not None and baseline[c][dim] is not None]
            lines.append(f"- {group[0]}/{group[1]} {dim}: {sum(deltas)/len(deltas) if deltas else 'UNSCORED'} (paired n={len(deltas)})")
    lines += ["", "Contract results are in each original run report; human scores do not replace contract checks.",
              "Mock runs remain diagnostic placeholders even if manually assigned human scores."]
    out = output_dir(args.output)
    save(out, "scores.json", {"schema": SCHEMA, "summary": summary})
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out / "report.md")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="offline actual Chatbot contracts by default")
    p.add_argument("--output", required=True)
    p.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    p.add_argument("--mode", choices=("offline", "llm", "import"), default="offline")
    p.add_argument("--label", default="candidate")
    p.add_argument("--replies")
    p.add_argument("--provider", choices=("openai", "ollama"))
    p.add_argument("--model")
    p.add_argument("--base-url")
    p.add_argument("--api-key-env", default="PERSONA_EVAL_API_KEY")
    p.add_argument("--temperature", type=float, default=0.4)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.set_defaults(func=run)
    p = sub.add_parser("blind", help="shuffle runs and policy names out of reviewer sheet")
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--seed", type=int, default=20250308)
    p.set_defaults(func=blind)
    p = sub.add_parser("score", help="validate human scores and produce coverage/paired report")
    p.add_argument("--sheet", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=score)
    args = parser.parse_args(argv)
    if getattr(args, "mode", None) == "import" and not args.replies:
        parser.error("--mode import requires --replies")
    if getattr(args, "mode", None) != "llm" and getattr(args, "provider", None):
        parser.error("--provider requires --mode llm")
    try:
        return args.func(args)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
