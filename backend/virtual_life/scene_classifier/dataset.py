"""Dataset normalization, filtering, de-duplication and deterministic splits."""

import json
import os
import random
import re
import tempfile
import unicodedata
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .labels import LABEL_INDEX, LABELS

MIN_TEXT_CHARS = 5
MAX_TEXT_CHARS = 200
DEFAULT_DEDUP_THRESHOLD = 0.9
SHINGLE_SIZE = 3
MAX_POSTING = 200
MAX_CANDIDATES = 2000

_BULLET = re.compile(r"^\s*(?:[-*•·—]|\d{1,3}[.、)）](?!\d))\s*")
_SPEAKER = re.compile(r"^\s*(?:用户|user|assistant|助手|我|你)\s*[:：]\s*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_URL = re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{12,}(?!\d)")
_HANDLE = re.compile(r"@[A-Za-z0-9_\-\u4e00-\u9fff]{2,32}")

_MINOR = re.compile(
    r"未成年|未满十八|未满18|幼女|幼童|儿童|小孩|萝莉|正太|小学生|初中生|高中生|"
    r"初中|小学|中学|underage|\bminor\b|\bchild\b|loli|shota",
    re.IGNORECASE,
)
_SEXUAL = re.compile(
    r"性交|性爱|性欲|做爱|上床|裸体|全裸|脱衣|色情|情欲|下体|私处|胸部|"
    r"抚摸|舌吻|湿吻|自慰|呻吟|高潮|插入|口交|乳|骚|发情|"
    r"\bsex\b|\bnsfw\b|\bnude\b|\bnaked\b|\borgasm\b",
    re.IGNORECASE,
)
_UNDERAGE_AGE = re.compile(r"(?<!\d)(?:[1-9]|1[0-7])\s*岁")


@dataclass(slots=True)
class Sample:
    text: str
    label: str
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        record = {**self.metadata, "text": self.text, "label": self.label}
        if self.source:
            record.setdefault("source", self.source)
        return record


def normalize_text(value: object) -> str:
    """Normalize user-visible text without rewriting its content."""

    if not isinstance(value, str):
        raise ValueError("text must be a string")
    # NFC keeps Chinese punctuation (NFKC rewrites ``，`` to an ASCII comma,
    # which changes how the text tokenizes and how it reads in artifacts).
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE.sub(" ", text).strip()
    for _ in range(2):
        stripped = _BULLET.sub("", text)
        stripped = _SPEAKER.sub("", stripped)
        if stripped == text:
            break
        text = stripped.strip()
    pairs = {
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("「", "」"),
        ("『", "』"),
    }
    while len(text) >= 2 and (text[0], text[-1]) in pairs:
        text = text[1:-1].strip()
    return text


def sanitize_pii(text: str) -> str:
    """Redact common identifiers.  This is a conservative baseline, not a DLP tool."""

    text = _URL.sub("[链接]", text)
    text = _EMAIL.sub("[邮箱]", text)
    text = _ID_CARD.sub("[证件号]", text)
    text = _PHONE.sub("[电话]", text)
    text = _HANDLE.sub("[账号]", text)
    return _LONG_NUMBER.sub("[号码]", text)


def contains_minor_content(text: str) -> bool:
    """Best-effort guard for the project's permanent red line.

    It only rejects text which combines a minor reference with sexual content
    (or an explicit age below 18).  It is a safety net for synthetic data, not
    a claim of complete detection.
    """

    minor = bool(_MINOR.search(text))
    if not minor:
        return bool(_UNDERAGE_AGE.search(text) and _SEXUAL.search(text))
    return bool(_SEXUAL.search(text))


def is_usable(
    text: str, *, min_chars: int = MIN_TEXT_CHARS, max_chars: int = MAX_TEXT_CHARS
) -> bool:
    return min_chars <= len(text) <= max_chars


def sample_from_record(record: Mapping[str, Any]) -> Sample:
    if not isinstance(record, Mapping):
        raise ValueError("each sample must be an object")
    text = record.get("text")
    label = record.get("label")
    if not isinstance(text, str):
        raise ValueError("sample.text must be a string")
    if not isinstance(label, str) or label not in LABEL_INDEX:
        raise ValueError(f"sample.label must be one of {list(LABELS)}")
    source = record.get("source", "")
    if not isinstance(source, str):
        source = str(source)
    metadata = {
        key: value for key, value in record.items() if key not in {"text", "label", "source"}
    }
    return Sample(text=text, label=label, source=source, metadata=dict(metadata))


def clean_records(
    records: Iterable[Mapping[str, Any]],
    *,
    min_chars: int = MIN_TEXT_CHARS,
    max_chars: int = MAX_TEXT_CHARS,
    redact: bool = False,
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    stats: Counter[str] | None = None,
) -> list[Sample]:
    """Normalize, filter, optionally redact and de-duplicate records.

    The near-duplicate check uses character shingles and an inverted index so
    the cost stays close to linear at the scale of this dataset.
    """

    if not 0.0 <= dedup_threshold <= 1.0:
        raise ValueError("dedup_threshold must be between 0 and 1")
    counters = stats if stats is not None else Counter()
    kept: list[Sample] = []
    seen: dict[str, int] = {}
    shingles: list[set[str]] = []
    index: dict[str, list[int]] = defaultdict(list)

    for record in records:
        counters["read"] += 1
        try:
            sample = sample_from_record(record)
        except ValueError:
            counters["invalid_record"] += 1
            continue
        try:
            text = normalize_text(sample.text)
        except ValueError:
            counters["invalid_record"] += 1
            continue
        if redact:
            text = sanitize_pii(text)
        if not is_usable(text, min_chars=min_chars, max_chars=max_chars):
            counters["length_rejected"] += 1
            continue
        if contains_minor_content(text):
            counters["minor_content_rejected"] += 1
            continue

        exact_key = text.casefold()
        if exact_key in seen:
            counters["exact_duplicate"] += 1
            continue
        sample.text = text
        sample_shingles = _shingles(text)
        duplicate_index = _find_near_duplicate(
            sample_shingles,
            shingles,
            index,
            threshold=dedup_threshold,
        )
        if duplicate_index is not None:
            counters["near_duplicate"] += 1
            continue
        sample_index = len(kept)
        seen[exact_key] = sample_index
        kept.append(sample)
        shingles.append(sample_shingles)
        if sample_shingles:
            for shingle in sample_shingles:
                postings = index[shingle]
                if len(postings) < MAX_POSTING:
                    postings.append(sample_index)
        counters["kept"] += 1

    return kept


def _shingles(text: str, size: int = SHINGLE_SIZE) -> set[str]:
    if len(text) <= size:
        return {text}
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _find_near_duplicate(
    candidate: set[str],
    existing: Sequence[set[str]],
    index: Mapping[str, Sequence[int]],
    *,
    threshold: float,
) -> int | None:
    if not candidate or not existing:
        return None
    overlaps: Counter[int] = Counter()
    for shingle in candidate:
        postings = index.get(shingle)
        if not postings or len(postings) > MAX_POSTING:
            continue
        for position in postings:
            overlaps[position] += 1
            if len(overlaps) >= MAX_CANDIDATES:
                break
    for position, shared in overlaps.most_common():
        other = existing[position]
        smaller = min(len(candidate), len(other))
        larger = max(len(candidate), len(other))
        if not smaller or larger / smaller < 0.8:
            # A short message contained in a long one is not a duplicate.
            continue
        union = len(candidate) + len(other) - shared
        if union and shared / union >= threshold:
            return position
        # Appending a suffix leaves Jaccard just below 0.9; containment catches
        # it without collapsing genuinely different short messages.
        if shared / smaller >= threshold:
            return position
    return None


def balance_samples(
    samples: Sequence[Sample], per_label: int | None, *, seed: int = 13
) -> list[Sample]:
    """Cap every label at ``per_label`` using a deterministic shuffle."""

    if per_label is None or per_label <= 0:
        return list(samples)
    rng = random.Random(seed)
    grouped: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.label].append(sample)
    output: list[Sample] = []
    for label in LABELS:
        group = grouped.get(label, [])
        rng.shuffle(group)
        output.extend(group[:per_label])
    rng.shuffle(output)
    return output


def split_samples(
    samples: Sequence[Sample],
    *,
    ratios: Sequence[float] = (0.7, 0.15, 0.15),
    seed: int = 13,
) -> dict[str, list[Sample]]:
    """Stratified, deterministic split.  Duplicates must be removed first."""

    if len(ratios) != 3:
        raise ValueError("ratios must contain train, validation and test values")
    if any(value < 0 for value in ratios) or abs(sum(ratios) - 1.0) > 1e-6:
        raise ValueError("ratios must be non-negative and sum to 1")
    train_ratio, val_ratio, test_ratio = (float(value) for value in ratios)
    grouped: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.label].append(sample)

    splits: dict[str, list[Sample]] = {"train": [], "val": [], "test": []}
    for label in LABELS:
        group = grouped.get(label, [])
        rng = random.Random(seed ^ zlib.crc32(label.encode("utf-8")))
        rng.shuffle(group)
        count = len(group)
        if count == 0:
            continue
        if count == 1:
            splits["train"].extend(group)
            continue
        if count == 2:
            splits["train"].append(group[0])
            splits["val"].append(group[1])
            continue
        val_count = max(1, int(round(count * val_ratio)))
        test_count = max(1, int(round(count * test_ratio)))
        while val_count + test_count >= count:
            if val_count >= test_count and val_count > 1:
                val_count -= 1
            elif test_count > 1:
                test_count -= 1
            else:
                break
        train_count = count - val_count - test_count
        splits["train"].extend(group[:train_count])
        splits["val"].extend(group[train_count : train_count + val_count])
        splits["test"].extend(group[train_count + val_count :])
    for group in splits.values():
        group.sort(key=lambda sample: (LABEL_INDEX[sample.label], sample.text))
    return splits


def dataset_stats(samples: Sequence[Sample]) -> dict[str, Any]:
    by_label = Counter(sample.label for sample in samples)
    return {
        "total": len(samples),
        "by_label": {label: by_label.get(label, 0) for label in LABELS},
        "mean_chars": (
            round(sum(len(sample.text) for sample in samples) / len(samples), 2) if samples else 0.0
        ),
    }


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from None
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: each line must be an object")
            records.append(record)
    return records


def write_jsonl(path: str | Path, records: Iterable[Mapping[str, Any]]) -> int:
    """Write atomically so an interrupted run cannot leave a partial split."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    descriptor, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True))
                stream.write("\n")
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o644)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return count
