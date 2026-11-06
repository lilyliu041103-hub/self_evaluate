"""Read-only AgentOS preflight and real IndustryBench data validation.

Does not install packages, change AgentOS, create sessions, or call paid models.
Run with a Python that has requests installed (only needed by upstream call_llm).
"""
import argparse
import collections
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import urllib.error
import urllib.request


def git_sha(path):
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentos-root", type=Path, required=True)
    parser.add_argument("--agentos-revision", required=True)
    parser.add_argument("--industrybench-revision", required=True)
    parser.add_argument("--industrybench-root", type=Path, default=Path(__file__).resolve().parents[1] / "third_party/IndustryBench")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root, bench, out = args.agentos_root, args.industrybench_root, args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    report = {"agentos_commit": args.agentos_revision, "industrybench_commit": args.industrybench_revision,
              "python": sys.version, "python_executable": sys.executable}
    report["modules"] = {m: importlib.util.find_spec(m) is not None for m in
                         ["uvicorn", "fastapi", "deepagents", "langchain_core", "langgraph", "pymysql", "dotenv", "requests"]}
    names = ["AGENTOS_LLM_AGENTOS_BASE_URL", "AGENTOS_LLM_AGENTOS_API_KEY",
             "AGENTOS_LLM_CHAT_API_KEY", "AGENTOS_DATABASE_HOST", "AGENTOS_DATABASE_USER",
             "AGENTOS_DATABASE_PASSWORD", "AGENTOS_DATABASE_NAME", "OPENAI_API_KEY", "DASHSCOPE_API_KEY"]
    report["env_present"] = {name: bool(os.environ.get(name)) for name in names}
    report["environment_files_exist"] = {str(p): p.exists() for p in [root / "agentos.env", Path(root.anchor) / "etc/agentos/agentos.env"]}
    report["local_ports"] = {}
    for port in [8005, 3306, 9000]:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                report["local_ports"][str(port)] = "accepting_connections"
        except OSError as exc:
            report["local_ports"][str(port)] = type(exc).__name__
    try:
        with urllib.request.urlopen("http://127.0.0.1:8005/api/v1/chat/sessions", timeout=3) as response:
            report["native_chat_http"] = {"status": response.status}
    except urllib.error.HTTPError as exc:
        report["native_chat_http"] = {"status": exc.code}
    except OSError as exc:
        report["native_chat_http"] = {"error": str(exc)}

    hub = "https://huggingface.co/api/datasets/alibaba-multimodal-industrial-ai/IndustryBench"
    with urllib.request.urlopen(hub, timeout=30) as response:
        metadata = json.load(response)
    revision = metadata["sha"]
    url = f"https://huggingface.co/datasets/alibaba-multimodal-industrial-ai/IndustryBench/resolve/{revision}/huggingface_dataset.csv"
    dataset_path = out / "industrybench.csv"
    with urllib.request.urlopen(url, timeout=60) as response:
        dataset_path.write_bytes(response.read())
    sys.path.insert(0, str(bench))
    evaluator = importlib.import_module("evaluate")
    rows = evaluator.load_dataset(str(dataset_path))
    ids = [row.get("id", "") for row in rows]
    report["dataset"] = {"revision": revision, "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                         "rows": len(rows), "columns": list(rows[0]), "unique_ids": len(set(ids)),
                         "missing_ids": ids.count(""), "missing_knowledge": sum(not r.get("knowledge_text", "").strip() for r in rows),
                         "languages": {lang: len(evaluator.extract_language_samples(rows, lang)) for lang in evaluator.LANGUAGE_FIELDS},
                         "capability_counts": dict(collections.Counter(r.get("capability", "") for r in rows)),
                         "difficulty_counts": dict(collections.Counter(r.get("difficulty", "") for r in rows))}
    sample = evaluator.extract_language_samples(rows, "zh")[0]
    # A non-secret placeholder is sent only to loopback, because upstream requires a key.
    # This is an endpoint probe, never a scored evaluation or an authentication bypass.
    report["official_client_loopback_probe"] = evaluator.call_llm(
        messages=[{"role": "user", "content": evaluator.LANGUAGE_PROMPTS["zh"].replace("${question}", sample.question)}],
        model="agentos-preflight", api_key="local-connectivity-probe-only",
        api_base="http://127.0.0.1:8005/v1", timeout=3)
    report["real_answers_generated"] = 0
    report["scored_samples"] = 0
    (out / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
