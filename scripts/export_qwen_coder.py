"""Merge the Anvil attention LoRA into its source Qwen2 GGUF for local Ollama."""
import argparse
import json
from pathlib import Path

import gguf
import numpy as np
from safetensors.numpy import load_file


def export(base, adapter, output):
    config = json.loads((adapter / "adapter_config.json").read_text(encoding="utf-8"))
    if (config["peft_type"] != "LORA" or config.get("use_rslora") or config.get("use_dora")
            or config.get("rank_pattern") or config.get("alpha_pattern") or config.get("fan_in_fan_out")):
        raise ValueError("Expected the unscaled-rank attention LoRA from train_qwen_coder.py")
    scale = config["lora_alpha"] / config["r"]
    reader = gguf.GGUFReader(str(base))
    if reader.get_field("general.architecture").contents() != "qwen2":
        raise ValueError("Expected a Qwen2 GGUF")
    layers = reader.get_field("qwen2.block_count").contents()
    mapping = gguf.get_tensor_name_map(gguf.MODEL_ARCH.QWEN2, layers)
    weights = load_file(str(adapter / "adapter_model.safetensors"))
    changes = {}
    for key, weight in weights.items():
        if key.endswith(".lora_B.weight"):
            continue
        stem = key.removeprefix("base_model.model.").removesuffix(".lora_A.weight")
        if not key.endswith(".lora_A.weight") or not stem.endswith((".q_proj", ".v_proj", ".o_proj")):
            raise ValueError(f"Unexpected adapter parameter: {key}")
        name = mapping.get_name(stem + ".weight", try_suffixes=(".weight",))
        other = key.replace(".lora_A.weight", ".lora_B.weight")
        changes[name] = (weight.astype(np.float32), weights[other].astype(np.float32))
    if len(weights) != len(changes) * 2 or set(changes) - {t.name for t in reader.tensors}:
        raise ValueError("Adapter tensor names do not match the base GGUF")

    partial = output.with_suffix(".partial.gguf")
    if output.exists() or partial.exists():
        raise FileExistsError(output if output.exists() else partial)
    writer = gguf.GGUFWriter(partial, "qwen2")
    for key, field in reader.fields.items():
        if key.startswith("GGUF.") or key in ("general.architecture", "general.name", "general.alignment"):
            continue
        subtype = field.types[1] if len(field.types) > 1 else None
        writer.add_key_value(key, field.contents(), field.types[0], subtype)
    writer.add_name("Anvil Qwen2.5 Coder 7B")
    writer.add_custom_alignment(reader.alignment)
    for tensor in reader.tensors:
        if tensor.name in changes:
            shape = tuple(reversed(tensor.shape.tolist()))
            a, b = changes[tensor.name]
            if (b.shape[0], a.shape[1]) != shape or a.shape[0] != b.shape[1]:
                raise ValueError(f"Adapter shape mismatch for {tensor.name}")
            writer.add_tensor_info(tensor.name, shape, np.dtype("float16"), 2 * int(np.prod(shape)))
        else:
            writer.add_tensor_info(tensor.name, tensor.data.shape, tensor.data.dtype,
                                   tensor.data.nbytes, raw_dtype=tensor.tensor_type)
    try:
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_ti_data_to_file()
        for tensor in reader.tensors:
            if tensor.name not in changes:
                writer.write_tensor_data(tensor.data)
                continue
            a, b = changes[tensor.name]
            value = gguf.dequantize(tensor.data, tensor.tensor_type).astype(np.float32, copy=True)
            for start in range(0, value.shape[0], 512):
                end = start + 512
                value[start:end] += scale * (b[start:end] @ a)
            writer.write_tensor_data(value.astype(np.float16))
    finally:
        writer.close()
    partial.rename(output)
    print(f"Merged {len(changes)} attention matrices into {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    export(options.base, options.adapter, options.output)
