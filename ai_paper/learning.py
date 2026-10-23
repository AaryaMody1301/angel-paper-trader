"""Causal five-minute datasets and optional, local-only LoRA training/inference."""

from collections import Counter
from dataclasses import asdict
import hashlib
from http.client import HTTPConnection, HTTPException
from importlib.metadata import version
import json
from pathlib import Path
import random
import subprocess
import shutil
import time

from ai_paper.market import (
    AIError,
    BAR,
    DIRECTIONS,
    SEGMENTS,
    canonical,
    config_id,
    digest,
    direction,
    instant,
    session_window,
)
from smartapi_data import IST

BASE_MODEL = "Qwen/Qwen3-0.6B"
SYSTEM = 'Predict the next completed five-minute close using only supplied data. Return only {"direction":"UP"}, {"direction":"DOWN"}, or {"direction":"FLAT"}.'
SCHEMA = {
    "type": "object",
    "properties": {"direction": {"type": "string", "enum": list(DIRECTIONS)}},
    "required": ["direction"],
    "additionalProperties": False,
}


class ContextPending(AIError):
    """The selected index's matching completed bar has not arrived yet."""


def context_contracts(store, config, segments):
    result = {}
    for segment in segments:
        selected = config.get("contexts", {}).get(segment)
        if selected is None:
            continue
        matches = [c for c in store.by_wire(selected["exchange"], selected["token"]) if c.kind == "INDEX"]
        if len(matches) != 1:
            raise AIError("configured analytical index identity is missing or ambiguous")
        result[segment] = matches[0]
    return result


def input_features(store, contract, bars, config):
    result = features(contract, bars, config)
    context = context_contracts(store, config, [contract.exchange]).get(contract.exchange)
    result["index_context"] = None
    if context:
        window = store.window(context.key, bars[-1]["timestamp"])
        if len(window) != 24 or window[-1]["timestamp"] != bars[-1]["timestamp"]:
            raise ContextPending("matching completed analytical index window is unavailable")
        features(context, window, config)
        close = window[-1]["ohlc"][3]
        result["index_context"] = {
            "instrument": context.key,
            "return_1_bps": close * 10000 // window[-2]["ohlc"][3] - 10000,
            "return_6_bps": close * 10000 // window[-7]["ohlc"][3] - 10000,
            "return_24_bps": close * 10000 // window[0]["ohlc"][0] - 10000,
        }
    return result


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(canonical(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def features(contract, bars, config):
    if len(bars) != 24:
        raise AIError("need exactly 24 completed consecutive bars")
    first, end = instant(bars[0]["timestamp"]), instant(bars[-1]["timestamp"]) + BAR
    day = first.astimezone(IST).date().isoformat()
    opened, closed = session_window(config, contract, day)
    if not opened <= first < end < closed:
        raise AIError("feature window crosses the exchange session")
    for index, bar in enumerate(bars):
        if bar.get("flags") or instant(bar["timestamp"]) != first + index * BAR:
            raise AIError("gapped, flagged or unfinished feature window")
        o, h, l, c = bar["ohlc"]
        if any(type(p) is not int or p <= 0 for p in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
            raise AIError("invalid feature OHLC")
        if type(bar["volume"]) is not int or bar["volume"] < 0:
            raise AIError("invalid feature volume")
    reference = bars[-1]["ohlc"][3]
    average_volume = max(1, sum(b["volume"] for b in bars) // 24)
    rows = [
        [(p * 10000 // reference) - 10000 for p in b["ohlc"]] + [b["volume"] * 100 // average_volume]
        for b in bars
    ]
    return {
        "segment": contract.exchange,
        "kind": contract.kind,
        "bars": rows,
        "contract": {
            "lot": contract.lot,
            "multiplier": contract.multiplier,
            "price_scale": contract.scale,
            "tick_units": contract.tick,
        },
        "minutes_after_open": int((end - opened).total_seconds() // 60),
        "expiry_minutes": None
        if contract.expiry is None
        else int((instant(contract.expiry) - end).total_seconds() // 60),
    }


def examples(store, config):
    result = []
    issues = config.get("data_issues", {})
    for key, c in store.contracts().items():
        if c.kind == "INDEX":
            continue
        rows = store.bars(key)
        for index in range(23, len(rows) - 1):
            window, following = rows[index - 23 : index + 1], rows[index + 1]
            at = instant(rows[index]["timestamp"])
            day = at.astimezone(IST).date().isoformat()
            if day in issues.get(key, []):
                continue
            try:
                f = input_features(store, c, window, config)
                if following["flags"] or instant(following["timestamp"]) != at + BAR:
                    continue
                _, closed = session_window(config, c, day)
                if at + 2 * BAR > closed:
                    continue
            except (ValueError, KeyError):
                continue
            close, target = rows[index]["ohlc"][3], following["ohlc"][3]
            label = direction(close, target)
            prior = rows[index - 1]["ohlc"][3]
            momentum = "UP" if close > prior else "DOWN" if close < prior else "FLAT"
            result.append(
                {
                    "instrument": key,
                    "segment": c.exchange,
                    "bar": at.isoformat(),
                    "day": day,
                    "target_end": (at + 2 * BAR).isoformat(),
                    "features": f,
                    "label": label,
                    "momentum": momentum,
                }
            )
    return sorted(result, key=lambda e: (e["bar"], e["instrument"]))


def complete_sessions(store, config):
    contracts = store.contracts()
    coverage = {key: set() for key in contracts}
    for key, c in contracts.items():
        by_day = {}
        for bar in store.bars(key):
            day = instant(bar["timestamp"]).astimezone(IST).date().isoformat()
            by_day.setdefault(day, []).append(bar)
        for day, bars in by_day.items():
            try:
                opened, closed = session_window(config, c, day)
                expected = [opened + i * BAR for i in range((closed - opened) // BAR)]
                if (
                    expected
                    and [instant(b["timestamp"]) for b in bars] == expected
                    and not any(b["flags"] for b in bars)
                ):
                    if day not in config.get("data_issues", {}).get(key, []):
                        coverage[key].add(day)
            except (KeyError, ValueError):
                continue
    return coverage


def prepare(store, config, directory):
    directory = Path(directory)
    if directory.exists():
        raise AIError("choose a new immutable dataset directory")
    rows = examples(store, config)
    if not rows:
        raise AIError("no causal examples; collect complete, verified sessions first")
    complete = complete_sessions(store, config)
    days = sorted({day for values in complete.values() for day in values})
    if len(days) < 100:
        raise AIError("need at least 100 complete dated sessions for the 60/20/20 split")
    train_end, validation_end = days[-41], days[-21]
    splits = {"train": [], "validation": [], "test": []}
    for row in rows:
        if row["day"] not in complete[row["instrument"]]:
            continue
        # Entire days and a full horizon separate boundaries, including across instruments.
        split = (
            "train" if row["day"] <= train_end else "validation" if row["day"] <= validation_end else "test"
        )
        splits[split].append(row)
    coverage = {
        segment: {
            split: len({r["day"] for r in values if r["segment"] == segment})
            for split, values in splits.items()
        }
        for segment in SEGMENTS
    }
    if not any(c["train"] >= 60 and c["validation"] >= 20 and c["test"] >= 20 for c in coverage.values()):
        raise AIError("no segment has 60/20/20 complete training, validation and test sessions")
    directory.mkdir(parents=True)
    hashes = {}
    for split, values in splits.items():
        path = directory / (split + ".jsonl")
        path.write_text("".join(canonical(r) + "\n" for r in values), encoding="utf-8")
        hashes[split] = file_hash(path)
    manifest = {
        "version": 1,
        "base_model": BASE_MODEL,
        "config_hash": config_id(config),
        "hashes": hashes,
        "coverage": coverage,
        "boundaries": [train_end, validation_end],
        "contracts": {k: asdict(c) for k, c in store.contracts().items()},
        "neutral_bps": 10,
        "feature_bars": 24,
        "horizon_minutes": 5,
        "source": "AUTHORISED_SMARTAPI",
        "calendars": {
            segment: {day: session for day, session in spec["sessions"].items() if day <= days[-1]}
            for segment, spec in config["segments"].items()
        },
    }
    manifest["id"] = digest(manifest)
    write_json(directory / "manifest.json", manifest)
    return manifest


def read_dataset(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    copy = {k: v for k, v in manifest.items() if k != "id"}
    if (
        digest(copy) != manifest["id"]
        or manifest.get("source") != "AUTHORISED_SMARTAPI"
        or set(manifest["hashes"]) != {"train", "validation", "test"}
    ):
        raise AIError("invalid dataset manifest")
    rows = {}
    for split, expected in manifest["hashes"].items():
        path = directory / (split + ".jsonl")
        if file_hash(path) != expected:
            raise AIError("dataset was modified")
        rows[split] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return manifest, rows


class TrainingBatches:
    """Tokenise one optimiser batch at a time instead of caching the whole corpus."""

    def __init__(self, tokenizer, rows):
        self.tokenizer, self.rows = tokenizer, rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        row = self.rows[index]
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": canonical(row["features"])},
        ]
        prefix = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        full = self.tokenizer.apply_chat_template(
            messages + [{"role": "assistant", "content": canonical({"direction": row["label"]})}],
            tokenize=False,
            enable_thinking=False,
        )
        tokens = self.tokenizer(full, return_tensors="pt", add_special_tokens=False)
        if tokens["input_ids"].shape[1] > 1024:
            raise AIError("training example exceeds 1024 tokens; data is never silently truncated")
        labels = tokens["input_ids"].clone()
        prefix_ids = self.tokenizer(prefix, add_special_tokens=False)["input_ids"]
        prefix_length = len(prefix_ids)
        if tokens["input_ids"][0, :prefix_length].tolist() != prefix_ids:
            raise AIError("chat template changed the assistant prefix; refusing incorrect supervision")
        labels[:, :prefix_length] = -100
        if not (labels != -100).any():
            raise AIError("chat template did not produce an assistant target")
        return {**tokens, "labels": labels}


def resolve_device(requested, torch):
    if requested not in ("auto", "cpu", "cuda"):
        raise AIError("device must be auto, cpu or cuda")
    if requested == "cuda" and not torch.cuda.is_available():
        raise AIError("CUDA requested but unavailable; install a CUDA build and compatible NVIDIA driver")
    return "cuda" if requested != "cpu" and torch.cuda.is_available() else "cpu"


def weight_digest(model, trainable):
    import torch

    h = hashlib.sha256()
    for name, parameter in model.named_parameters():
        if parameter.requires_grad == trainable:
            h.update(name.encode())
            h.update(parameter.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes())
    return h.hexdigest()


def fit_lora(model, batches, device, *, steps=None, accumulation=16):
    import psutil
    import torch

    if not batches:
        raise AIError("no training examples")
    model.to(device)
    model.train()
    frozen, adapter = weight_digest(model, False), weight_digest(model, True)
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=0.0002)
    before, peak, losses, count = time.monotonic(), 0, [], 0
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    for offset in range(0, len(batches), accumulation):
        optimizer.zero_grad(set_to_none=True)
        group = batches[offset : offset + accumulation]
        for batch in group:
            output = model(**{k: v.to(device) for k, v in batch.items()})
            loss = output.loss
            if not torch.isfinite(loss):
                raise AIError("non-finite training loss")
            (loss / len(group)).backward()
            losses.append(float(loss.detach().cpu()))
            peak = max(peak, psutil.Process().memory_info().rss)
        torch.nn.utils.clip_grad_norm_(
            (p for p in model.parameters() if p.requires_grad), 1.0, error_if_nonfinite=True
        )
        optimizer.step()
        if any(not torch.isfinite(p).all() for p in model.parameters() if p.requires_grad):
            raise AIError("optimizer produced non-finite adapter weights")
        count += 1
        if steps is not None and count >= steps:
            break
    if frozen != weight_digest(model, False) or adapter == weight_digest(model, True):
        raise AIError("training changed frozen weights or failed to update the adapter")
    training_seconds = time.monotonic() - before
    model.eval()
    first = batches[0]
    prefix = int((first["labels"][0] == -100).sum().item()) or first["input_ids"].shape[1]
    started = time.monotonic()
    with torch.no_grad():
        model.generate(
            input_ids=first["input_ids"][:, :prefix].to(device),
            attention_mask=first["attention_mask"][:, :prefix].to(device),
            max_new_tokens=32,
            do_sample=False,
            pad_token_id=model.config.eos_token_id,
        )
    if device == "cuda":
        torch.cuda.synchronize()
    memory = psutil.Process().memory_info()
    peak = max(peak, memory.rss, getattr(memory, "peak_wset", 0))
    import sys

    if sys.platform != "win32":
        import resource

        peak = max(
            peak,
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        )
    return {
        "device": device,
        "steps": count,
        "seconds": training_seconds,
        "steps_per_second": count / training_seconds,
        "peak_rss_bytes": peak,
        "inference_seconds": time.monotonic() - started,
        "peak_cuda_bytes": torch.cuda.max_memory_allocated() if device == "cuda" else None,
        "loss": losses[-1],
        "frozen_weights_unchanged": True,
        "adapter_changed": True,
    }


def train(directory, output, device="auto", benchmark_only=False, gguf_tools=None, import_ollama=False):
    if gguf_tools and not (Path(gguf_tools) / "convert_hf_to_gguf.py").is_file():
        raise AIError("--gguf-tools must point to a reviewed llama.cpp checkout")
    if import_ollama and (not gguf_tools or shutil.which("ollama") is None):
        raise AIError("--import-ollama requires --gguf-tools and an installed Ollama executable")
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import LoraConfig, get_peft_model
    except ImportError:
        raise AIError("install requirements-training.txt in a training environment") from None
    manifest, rows = read_dataset(directory)
    chosen = resolve_device(device, torch)
    output = Path(output)
    if output.exists():
        raise AIError("choose a new model artifact directory")
    random.seed(42)
    torch.manual_seed(42)
    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        torch_dtype=torch.float32,
        trust_remote_code=False,
        use_safetensors=True,
        token=False,
        cache_dir=".hf-cache",
    )
    revision = base.config._commit_hash
    if not revision:
        raise AIError("base model revision could not be resolved")
    tokenizer = AutoTokenizer.from_pretrained(
        BASE_MODEL, revision=revision, trust_remote_code=False, token=False, cache_dir=".hf-cache"
    )
    model = get_peft_model(
        base,
        LoraConfig(
            r=8, lora_alpha=16, lora_dropout=0.05, target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM"
        ),
    )
    random.shuffle(rows["train"])
    batches = TrainingBatches(tokenizer, rows["train"])
    stats = fit_lora(model, batches, chosen, steps=20 if benchmark_only else None)
    output.mkdir(parents=True)
    model.save_pretrained(output / "adapter", safe_serialization=True)
    tokenizer.save_pretrained(output / "adapter")
    metadata = {
        "version": 1,
        "base_model": BASE_MODEL,
        "base_revision": revision,
        "dataset": manifest["id"],
        "config_hash": manifest["config_hash"],
        "benchmark_only": benchmark_only,
        "stats": stats,
        "parameters": {
            "rank": 8,
            "alpha": 16,
            "dropout": 0.05,
            "learning_rate": 0.0002,
            "batch": 1,
            "accumulation": 16,
            "max_tokens": 1024,
            "epochs": 1,
            "seed": 42,
        },
        "environment": {p: version(p) for p in ("torch", "transformers", "peft", "accelerate", "psutil")},
    }
    metadata["adapter_hashes"] = {p.name: file_hash(p) for p in (output / "adapter").glob("*.safetensors")}
    metadata["id"] = digest(metadata)
    write_json(output / "model.json", metadata)
    merged = model.merge_and_unload(safe_merge=True)
    merged.save_pretrained(output / "merged", safe_serialization=True)
    tokenizer.save_pretrained(output / "merged")
    if gguf_tools:
        import sys

        root = Path(gguf_tools).resolve()
        gguf = (output / "model.f16.gguf").resolve()
        subprocess.run(
            [
                sys.executable,
                str(root / "convert_hf_to_gguf.py"),
                str((output / "merged").resolve()),
                "--outfile",
                str(gguf),
                "--outtype",
                "f16",
            ],
            check=True,
        )
        metadata["gguf_hash"] = file_hash(gguf)
        if import_ollama:
            modelfile = output / "Modelfile"
            modelfile.write_text(
                'FROM "' + str(gguf).replace("\\", "/") + '"\nPARAMETER temperature 0\n', encoding="utf-8"
            )
            name = "angel-paper:" + metadata["id"][:12]
            subprocess.run(["ollama", "create", name, "-f", str(modelfile.resolve())], check=True)
            metadata["ollama_name"], metadata["ollama_digest"] = name, Ollama(name).model_digest()
        write_json(output / "model.json", metadata)
    return metadata


class Ollama:
    """Fixed loopback HTTP; no provider keys, cloud models, tools or redirects."""

    def __init__(self, model, timeout=10):
        if (
            not isinstance(model, str)
            or not model
            or len(model) > 128
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/-" for c in model
            )
        ):
            raise AIError("invalid local model name")
        self.model, self.timeout, self.pinned_digest = model, timeout, None

    def _call(self, path, payload=None):
        connection = HTTPConnection("127.0.0.1", 11434, timeout=self.timeout)
        try:
            connection.request(
                "GET" if payload is None else "POST",
                path,
                body=None if payload is None else canonical(payload),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(body) > 1024 * 1024:
                raise AIError("local Ollama request failed or exceeded response limit")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise AIError("local Ollama returned an invalid response shape")
            return value
        except (OSError, HTTPException, UnicodeError, json.JSONDecodeError):
            raise AIError("local Ollama unavailable or returned invalid data") from None
        finally:
            connection.close()

    def model_digest(self):
        show = self._call("/api/show", {"model": self.model})
        if (
            show.get("remote_host")
            or show.get("remote_model")
            or not isinstance(show.get("details"), dict)
            or show["details"].get("format") != "gguf"
        ):
            raise AIError("only an installed local GGUF model is allowed")
        models = self._call("/api/tags").get("models")
        if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
            raise AIError("local model listing is invalid")
        matches = [m for m in models if m.get("name") == self.model]
        if len(matches) != 1 or len(matches[0].get("digest", "")) != 64:
            raise AIError("local model digest is unavailable")
        value = matches[0]["digest"]
        if self.pinned_digest is not None and value != self.pinned_digest:
            raise AIError("local model changed during the session")
        self.pinned_digest = value
        return value

    def predict(self, value):
        self.model_digest()
        result = self._call(
            "/api/chat",
            {
                "model": self.model,
                "stream": False,
                "think": False,
                "format": SCHEMA,
                "options": {"temperature": 0, "num_predict": 32, "seed": 42, "num_ctx": 2048},
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": canonical(value)},
                ],
            },
        )
        if result.get("done") is not True or result.get("done_reason") != "stop":
            raise AIError("local inference was truncated or unfinished")
        message = result.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise AIError("local inference returned an invalid message")
        answer = json.loads(message["content"])
        if (
            not isinstance(answer, dict)
            or set(answer) != {"direction"}
            or answer["direction"] not in DIRECTIONS
        ):
            raise AIError("model must return only one valid direction")
        return answer["direction"]


def macro_f1(actual, predicted):
    if len(actual) != len(predicted) or not actual:
        raise AIError("missing evaluation predictions")
    scores = []
    for label in DIRECTIONS:
        tp = sum(a == p == label for a, p in zip(actual, predicted))
        fp = sum(a != label and p == label for a, p in zip(actual, predicted))
        fn = sum(a == label and p != label for a, p in zip(actual, predicted))
        scores.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0)
    return sum(scores) / 3


def forecast_metrics(train_rows, rows, predictions, baseline):
    majority = Counter(r["label"] for r in train_rows).most_common(1)[0][0]
    actual = [r["label"] for r in rows]
    return {
        "model": macro_f1(actual, predictions),
        "majority": macro_f1(actual, [majority] * len(rows)),
        "momentum": macro_f1(actual, [r["momentum"] for r in rows]),
        "base_model": macro_f1(actual, baseline),
        "accuracy": sum(a == p for a, p in zip(actual, predictions)) / len(rows),
        "count": len(rows),
    }
