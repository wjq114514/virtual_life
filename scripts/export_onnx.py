#!/usr/bin/env python
"""Export the trained checkpoint to ONNX and optionally dynamic INT8."""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch
from transformers import BertForSequenceClassification, BertTokenizer
from virtual_life.scene_classifier.artifact import (
    MANIFEST_NAME,
    read_manifest,
    utc_now,
    write_manifest,
)
from virtual_life.scene_classifier.dataset import read_jsonl
from virtual_life.scene_classifier.labels import LABELS

DEFAULT_ARTIFACT = Path(".data/nsfw_classifier") / "model" / "final"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--output", default="", help="output directory (default: artifact)")
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--no-quantize", dest="quantize", action="store_false", default=True)
    parser.add_argument("--max-length", type=int, default=128)
    args = parser.parse_args(argv)
    if not 12 <= args.opset <= 20:
        parser.error("opset must be between 12 and 20")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import onnxruntime as ort
    except ImportError:
        print("onnxruntime is required: uv sync --group train", file=sys.stderr)
        return 2

    artifact = Path(args.artifact)
    if not (artifact / "config.json").exists():
        print(f"not a classifier checkpoint: {artifact}", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else artifact
    output.mkdir(parents=True, exist_ok=True)

    tokenizer = BertTokenizer.from_pretrained(str(artifact))
    model = BertForSequenceClassification.from_pretrained(str(artifact))
    model.eval()
    encoded = tokenizer(
        ["你好，今天过得怎么样？", "我有点难过，想和你说说话"],
        padding=True,
        truncation=True,
        max_length=args.max_length,
        return_tensors="pt",
    )
    inputs = tuple(encoded[name] for name in ("input_ids", "attention_mask"))
    parity_inputs = _parity_inputs(tokenizer, artifact, args.max_length)
    with torch.no_grad():
        reference = model(*parity_inputs).logits.detach().cpu().numpy()
        torch.onnx.export(
            model,
            inputs,
            str(output / "model.onnx"),
            input_names=["input_ids", "attention_mask"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "sequence"},
                "attention_mask": {0: "batch", 1: "sequence"},
                "logits": {0: "batch"},
            },
            opset_version=args.opset,
            do_constant_folding=True,
            dynamo=False,
        )
    fp32_path = output / "model.onnx"
    parity = _run_onnx(ort, fp32_path, parity_inputs)
    fp32_max_diff = float(np.max(np.abs(parity - reference)))
    if fp32_max_diff > 1e-4:
        print(f"FP32 ONNX parity failed: max diff {fp32_max_diff}", file=sys.stderr)
        return 1

    summary = {
        "fp32": str(fp32_path),
        "opset": args.opset,
        "parity_samples": int(parity_inputs[0].shape[0]),
        "fp32_max_abs_diff": fp32_max_diff,
        "fp32_argmax_equal": bool((parity.argmax(axis=1) == reference.argmax(axis=1)).all()),
    }
    if args.quantize:
        from onnxruntime.quantization import QuantType, quantize_dynamic

        int8_path = output / "model.int8.onnx"
        # QUInt8 is materially more accurate than QInt8 for this BERT export on
        # x86 CPUs; QInt8 collapsed the test-set accuracy in practice.
        quantize_dynamic(str(fp32_path), str(int8_path), weight_type=QuantType.QUInt8)
        quantized = _run_onnx(ort, int8_path, parity_inputs)
        agreement = float((quantized.argmax(axis=1) == reference.argmax(axis=1)).mean())
        verified = agreement >= 0.95
        summary.update(
            {
                "int8": str(int8_path),
                "int8_max_abs_diff": float(np.max(np.abs(quantized - reference))),
                "int8_argmax_agreement": agreement,
                "int8_argmax_equal": bool(agreement == 1.0),
                "int8_verified": verified,
            }
        )
        if not verified:
            print(
                "INT8 export diverged from FP32 on the parity set; "
                "the service will keep FP32 for production",
                file=sys.stderr,
            )

    source_manifest = artifact / MANIFEST_NAME
    manifest = read_manifest(source_manifest) if source_manifest.exists() else {}
    manifest_path = output / MANIFEST_NAME
    manifest["labels"] = list(LABELS)
    manifest["onnx"] = {**summary, "exported_at": utc_now()}
    write_manifest(manifest_path, manifest)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _parity_inputs(tokenizer, artifact: Path, max_length: int):
    """Use a representative slice of held-out predictions for parity checks."""

    texts: list[str] = []
    for name in ("test_predictions.jsonl", "val_predictions.jsonl"):
        path = artifact / name
        if not path.exists():
            continue
        for record in read_jsonl(path):
            text = record.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
            if len(texts) >= 64:
                break
        if texts:
            break
    if not texts:
        texts = ["你好，今天过得怎么样？", "我有点难过，想和你说说话"]
    encoded = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    return tuple(encoded[name] for name in ("input_ids", "attention_mask"))


def _run_onnx(ort, path: Path, inputs) -> np.ndarray:
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    feed = {
        "input_ids": inputs[0].numpy().astype(np.int64),
        "attention_mask": inputs[1].numpy().astype(np.int64),
    }
    return np.asarray(session.run(None, feed)[0])


if __name__ == "__main__":
    raise SystemExit(main())
