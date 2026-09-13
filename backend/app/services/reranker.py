import json
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
from tokenizers import Tokenizer
from tokenizers.pre_tokenizers import Whitespace
from onnxruntime import InferenceSession

logger = logging.getLogger(__name__)

RERANKER_MODEL_DIR = Path("/app/models/reranker")
RERANKER_MODEL_FILE = RERANKER_MODEL_DIR / "model.onnx"
RERANKER_TOKENIZER_FILE = RERANKER_MODEL_DIR / "tokenizer.json"
RERANKER_CONFIG_FILE = RERANKER_MODEL_DIR / "config.json"

_session: InferenceSession | None = None
_tokenizer: Tokenizer | None = None
_config: dict[str, Any] | None = None


def _load_resources() -> tuple[InferenceSession, Tokenizer, dict[str, Any]]:
    global _session, _tokenizer, _config
    if _session is None:
        if not RERANKER_MODEL_FILE.exists() or not RERANKER_TOKENIZER_FILE.exists():
            raise FileNotFoundError(
                f"ONNX reranker model not found in {RERANKER_MODEL_DIR}. "
                "Ensure the Docker image is built with the model downloaded."
            )
        _session = InferenceSession(str(RERANKER_MODEL_FILE))
        _tokenizer = Tokenizer.from_file(str(RERANKER_TOKENIZER_FILE))
        _tokenizer.pre_tokenizer = Whitespace()
        if RERANKER_CONFIG_FILE.exists():
            with open(RERANKER_CONFIG_FILE, "r") as f:
                _config = json.load(f)
        else:
            _config = {"max_length": 512}
    return _session, _tokenizer, _config


def _tokenize_pair(tokenizer: Tokenizer, query: str, passage: str, max_length: int) -> dict[str, Any]:
    text = f"[CLS] {query} [SEP] {passage} [SEP]"
    encoding = tokenizer.encode(text)
    input_ids = encoding.ids[:max_length]
    attention_mask = encoding.attention_mask[:max_length]

    pad_len = max_length - len(input_ids)
    input_ids = input_ids + [0] * pad_len
    attention_mask = attention_mask + [0] * pad_len

    return {
        "input_ids": np.array([input_ids], dtype=np.int64),
        "attention_mask": np.array([attention_mask], dtype=np.int64),
    }


def score_pairs(question: str, passages: list[str], batch_size: int = 8) -> list[float]:
    if not passages:
        return []

    session, tokenizer, config = _load_resources()
    max_length = config.get("max_length", 512)

    all_scores: list[float] = []
    for i in range(0, len(passages), batch_size):
        batch = passages[i : i + batch_size]
        input_ids_list = []
        attention_mask_list = []

        for passage in batch:
            tokens = _tokenize_pair(tokenizer, question, passage, max_length)
            input_ids_list.append(tokens["input_ids"])
            attention_mask_list.append(tokens["attention_mask"])

        input_ids = np.concatenate(input_ids_list, axis=0)
        attention_mask = np.concatenate(attention_mask_list, axis=0)

        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }

        outputs = session.run(None, inputs)
        logits = outputs[0]

        for j in range(len(batch)):
            score = float(logits[j][0])
            all_scores.append(score)

    return all_scores
