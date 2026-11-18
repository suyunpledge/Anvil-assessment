"""Train an Anvil-only LoRA with a local NF4 Qwen2.5-Coder-7B-Instruct checkpoint."""
from __future__ import annotations

import argparse
import ast
import json
import math
import random
import time
from pathlib import Path

import bitsandbytes as bnb
import torch
import torch.nn.functional as F
from peft import LoraConfig, get_peft_model
from peft.utils import get_peft_model_state_dict, set_peft_model_state_dict
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from qwen_training_data import adapter_for_data, evaluate_cases, heldout_cases, training_cases


def load_gguf_for_training(path, base, dtype):
    """Read Qwen2 GGUF one tensor at a time, then freeze a CUDA NF4 copy."""
    import gguf
    from accelerate import init_empty_weights
    from transformers.integrations.bitsandbytes import replace_with_bnb_linear
    from transformers.modeling_gguf_pytorch_utils import get_gguf_hf_weights_map

    reader = gguf.GGUFReader(str(path))
    config = AutoConfig.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    if config.model_type != "qwen2" or reader.get_field("general.architecture").contents() != "qwen2":
        raise ValueError("Expected matching Qwen2 model config and GGUF")
    with init_empty_weights():
        model = AutoModelForCausalLM.from_config(config, dtype=dtype, attn_implementation="sdpa")
    expected = {name: tuple(weight.shape) for name, weight in model.named_parameters()}
    mapping = get_gguf_hf_weights_map(model, model_type="qwen2", num_layers=config.num_hidden_layers)
    if {mapping.get(t.name) for t in reader.tensors} != set(expected):
        raise ValueError("GGUF tensor names do not match the local Qwen config")
    model = replace_with_bnb_linear(model, quantization_config=BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=dtype))
    device = torch.device("cuda:0")
    with torch.no_grad():
        for index, tensor in enumerate(reader.tensors, 1):
            name = mapping[tensor.name]
            shape = tuple(reversed(tensor.shape.tolist()))
            if shape != expected[name]:
                raise ValueError(f"{tensor.name}: GGUF shape {shape} != {expected[name]}")
            module_name, attribute = name.rsplit(".", 1)
            module = model.get_submodule(module_name)
            dense = torch.empty(shape, dtype=dtype, device=device)
            # The embedding and output tensors exceed 2 GB in float32; never expand them at once.
            for offset in range(0, shape[0], 512):
                chunk = gguf.dequantize(tensor.data[offset:offset + 512], tensor.tensor_type)
                dense[offset:offset + len(chunk)].copy_(torch.from_numpy(chunk.copy()))
            if isinstance(module, bnb.nn.Linear4bit) and attribute == "weight":
                data, state = bnb.functional.quantize_4bit(dense, quant_type="nf4", compress_statistics=True)
                module.weight = bnb.nn.Params4bit.from_prequantized(
                    data, state.as_dict(packed=True), requires_grad=False, device=device, module=module)
            else:
                setattr(module, attribute, torch.nn.Parameter(dense, requires_grad=False))
            del dense
            if index % 56 == 0 or index == len(reader.tensors):
                print(f"Loaded {index}/{len(reader.tensors)} GGUF tensors", flush=True)
        for module in model.modules():
            for name, buffer in module.named_buffers(recurse=False):
                setattr(module, name, buffer.to(device))
    model.is_loaded_in_4bit = True
    return model


def encode(tokenizer, case, adapter, max_tokens):
    messages, tools = case.prompt(adapter)
    prompt = tokenizer.apply_chat_template(messages, tools=tools, tokenize=False, add_generation_prompt=True)
    conversation = tokenizer.apply_chat_template(
        messages + [case.completion()], tools=tools, tokenize=False, add_generation_prompt=False)
    if not conversation.startswith(prompt):
        raise ValueError(f"{case.name}: assistant boundary does not match the chat template")
    context = tokenizer.encode(prompt, add_special_tokens=False)
    response = tokenizer.encode(conversation[len(prompt):].rstrip("\n"), add_special_tokens=False)
    if not response or len(context) + len(response) > max_tokens:
        raise ValueError(f"{case.name}: {len(context)}+{len(response)} tokens exceed {max_tokens}")
    return context, response


def completion_loss(model, sample):
    context, response = sample
    device = model.get_input_embeddings().weight.device
    tokens = torch.tensor([context + response[:-1]], device=device)
    positions = torch.arange(len(context) - 1, tokens.size(1), device=device)
    logits = model(input_ids=tokens, use_cache=False, logits_to_keep=positions).logits[0]
    targets = torch.tensor(response, device=device)
    return F.cross_entropy(logits.float(), targets)


@torch.no_grad()
def validation_loss(model, samples):
    model.eval()
    return sum(float(completion_loss(model, sample)) for sample in samples) / len(samples)


class GenerateTransport:
    """Use Anvil's adapter decoder with the training runtime for a fair A/B comparison."""
    def __init__(self, model, tokenizer):
        self.model = model
        self.tokenizer = tokenizer

    @torch.no_grad()
    def chat(self, body, timeout=None):
        started = time.monotonic()
        inputs = self.tokenizer.apply_chat_template(
            body["messages"], tools=body.get("tools"), tokenize=True, add_generation_prompt=True,
            return_tensors="pt", return_dict=True).to(self.model.get_input_embeddings().weight.device)
        self.model.eval()
        output = self.model.generate(**inputs, do_sample=False, max_new_tokens=160, use_cache=True,
                                     pad_token_id=self.tokenizer.pad_token_id)
        generated = output[0, inputs["input_ids"].size(1):]
        decoded = self.tokenizer.decode(generated, skip_special_tokens=True)
        return {"message": {"content": decoded}}, time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path, help="local Qwen config and tokenizer or 4-bit checkpoint")
    parser.add_argument("--gguf", type=Path, help="use the existing Qwen2.5-Coder GGUF weights instead of safetensors")
    parser.add_argument("--output", required=True, type=Path, help="new run directory")
    parser.add_argument("--steps", type=int, default=48)
    parser.add_argument("--max-tokens", type=int, default=1536)
    parser.add_argument("--seed", type=int, default=917)
    parser.add_argument("--learning-rate", type=float, default=8e-5)
    parser.add_argument("--resume", action="store_true", help="continue an interrupted run in --output")
    parser.add_argument("--baseline", type=Path, help="reuse a baseline from the identical model and holdout set")
    parser.add_argument("--save-every", type=int, default=5, help="training steps between recovery checkpoints")
    args = parser.parse_args()
    if args.steps < 1 or args.max_tokens < 1 or args.save_every < 1 or not (0 < args.learning_rate < 1):
        parser.error("steps, max-tokens, save-every and learning-rate must be positive")
    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA PyTorch environment is required")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    recovery = args.output / "recovery.pt"
    baseline_path = args.output / "baseline.json"
    metadata = {"base": str(args.base.resolve()), "gguf": str(args.gguf.resolve()) if args.gguf else None,
                "seed": args.seed, "max_tokens": args.max_tokens, "learning_rate": args.learning_rate}
    state = torch.load(recovery, map_location="cpu", weights_only=True) if args.resume and recovery.is_file() else None
    if state is not None and (state["settings"] != metadata or len(state["losses"]) > args.steps):
        raise ValueError("Recovery checkpoint settings do not match this run")
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.base, local_files_only=True, trust_remote_code=False)
    adapter = adapter_for_data()
    train = training_cases()
    heldout = heldout_cases()
    train_tokens = [encode(tokenizer, case, adapter, args.max_tokens) for case in train]
    validation = [encode(tokenizer, case, adapter, args.max_tokens) for case in heldout]
    for case in train + heldout:
        ast.parse(case.expected_source(), filename=case.target)
    print(f"Train {len(train)}, held out {len(heldout)}, max tokens "
          f"{max(sum(map(len, sample)) for sample in train_tokens + validation)}", flush=True)
    print(f"Loading local checkpoint on {torch.cuda.get_device_name()}", flush=True)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    if args.gguf:
        model = load_gguf_for_training(args.gguf, args.base, dtype)
    else:
        model = AutoModelForCausalLM.from_pretrained(
            args.base, local_files_only=True, trust_remote_code=False, use_safetensors=True,
            device_map={"": 0}, dtype=dtype, attn_implementation="sdpa")
    if not getattr(model, "is_loaded_in_4bit", False) or model.config.model_type != "qwen2":
        raise RuntimeError("Expected a prequantized Qwen2.5 Coder checkpoint")
    for layer in model.modules():
        if isinstance(layer, bnb.nn.Linear4bit):
            layer.compute_dtype = dtype
    model.requires_grad_(False)
    model = get_peft_model(model, LoraConfig(task_type="CAUSAL_LM", r=8, lora_alpha=16,
                                            target_modules=["q_proj", "v_proj", "o_proj"]))
    model.print_trainable_parameters()
    parameters = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(parameters, lr=args.learning_rate, foreach=False)
    adapter.transport = GenerateTransport(model, tokenizer)
    cached_baseline = baseline_path if args.resume and baseline_path.is_file() else args.baseline
    if cached_baseline:
        stored = json.loads(cached_baseline.read_text(encoding="utf-8"))
        baseline, before = float(stored["loss"]), stored["cases"]
        if not math.isfinite(baseline) or [row["case"] for row in before] != [case.name for case in heldout]:
            raise ValueError("Cached baseline does not match the held-out cases")
        print(f"Reusing baseline from {cached_baseline}", flush=True)
    else:
        print("Baseline held-out inference", flush=True)
        baseline = validation_loss(model, validation)
        before = evaluate_cases(adapter, heldout)
    print(f"Baseline held-out loss {baseline:.4f}", flush=True)
    baseline_tmp = baseline_path.with_suffix(".tmp")
    baseline_tmp.write_text(json.dumps({"loss": baseline, "cases": before},
        ensure_ascii=False, indent=2), encoding="utf-8")
    baseline_tmp.replace(baseline_path)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()
    started = time.monotonic()
    order = list(range(len(train)))
    schedule = []
    while len(schedule) < args.steps:
        rng.shuffle(order)
        schedule.extend(order)
    losses = state["losses"] if state else []
    elapsed = state["train_seconds"] if state else 0.0
    if state:
        expected_keys = get_peft_model_state_dict(model).keys()
        if state["adapter"].keys() != expected_keys:
            raise ValueError("Recovery checkpoint adapter tensors do not match the model")
        set_peft_model_state_dict(model, state["adapter"])
        opt.load_state_dict(state["optimizer"])
        del state
        print(f"Resuming at step {len(losses)}/{args.steps}", flush=True)
    for step in range(len(losses), args.steps):
        idx = schedule[step]
        opt.zero_grad(set_to_none=True)
        loss = completion_loss(model, train_tokens[idx])
        value = float(loss.detach())
        if not math.isfinite(value):
            raise RuntimeError(f"Nonfinite loss at step {step + 1}")
        loss.backward()
        torch.nn.utils.clip_grad_norm_(parameters, max_norm=1.0, error_if_nonfinite=True)
        opt.step()
        losses.append(value)
        print(f"step {step + 1}/{args.steps} loss={value:.4f} case={train[idx].name}", flush=True)
        if (step + 1) % args.save_every == 0 or step + 1 == args.steps:
            tmp = recovery.with_suffix(".tmp")
            torch.save({"settings": metadata, "losses": losses, "train_seconds": elapsed + time.monotonic() - started,
                        "adapter": get_peft_model_state_dict(model), "optimizer": opt.state_dict()}, tmp)
            tmp.replace(recovery)
    model.gradient_checkpointing_disable()
    train_seconds = elapsed + time.monotonic() - started
    checkpoint = args.output / "adapter"
    model.save_pretrained(checkpoint, safe_serialization=True)
    print("Trained held-out inference", flush=True)
    final = validation_loss(model, validation)
    after = evaluate_cases(adapter, heldout)
    report = {"base": str(args.base), "gguf": str(args.gguf) if args.gguf else None,
              "steps": args.steps, "seed": args.seed,
              "learning_rate": args.learning_rate, "train_examples": len(train), "heldout_examples": len(heldout),
              "trainable_parameters": sum(p.numel() for p in parameters), "losses": losses,
              "train_seconds": train_seconds, "peak_cuda_mib": torch.cuda.max_memory_reserved() / 2**20,
              "before_loss": baseline, "after_loss": final, "before": before, "after": after}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Held-out loss {baseline:.4f} -> {final:.4f}, cases "
          f"{sum(row['passed'] for row in before)}/{len(heldout)} -> "
          f"{sum(row['passed'] for row in after)}/{len(heldout)}", flush=True)


if __name__ == "__main__":
    main()
