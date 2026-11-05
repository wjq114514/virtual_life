"""Conservative typo generator (MaiBot typo_generator port, prompt-free).

EXPERIMENTAL humanization mechanism, OFF by default (dialogue.typo_enabled).
When enabled it may — with low probability — replace exactly one character in
the final reply with a near-sound neighbour from a small hand-curated table of
typos real Chinese typists actually make. It deliberately violates the general
"never touch the final reply text" principle, which is why it:

  * is a separate, independently switchable config flag (default false),
  * applies at most ONE substitution per reply,
  * only fires in high-arousal positive moods (cheerful / amused / excited),
  * is deterministic per (companion_id, command_id), so receipts replay exactly.

This is a pure-function module: no storage, no model calls, no I/O.
"""

import random

# Minimum reply length eligible for a typo. Very short replies have no room
# for a believable slip, and mangling one would dominate the message.
_MIN_LENGTH = 6

# High-arousal moods in which a typo is believable; low-arousal replies stay
# clean. The dialogue layer enforces this before calling apply_typo().
TYPO_MOODS = frozenset({"cheerful", "amused", "excited"})

# Only pairs real humans mistype at high frequency. Directional (first →
# second) is not enforced: substitution picks the *other* member of the pair
# whichever appears in the text. No full pinyin mapping, no rare pairs.
_HOMOPHONE_PAIRS: tuple[tuple[str, str], ...] = (
    ("的", "地"),
    ("的", "得"),
    ("地", "得"),
    ("再", "在"),
    ("做", "作"),
    ("哪", "那"),
    ("以", "已"),
    ("须", "需"),
    ("像", "向"),
    ("象", "像"),
    ("在", "再"),
    ("由", "又"),
    ("和", "或"),
    ("既", "即"),
    ("既", "暨"),
    ("因", "应"),
    ("应", "因"),
    ("他", "她"),
    ("她", "他"),
    ("那", "哪"),
    ("作", "做"),
    ("需", "须"),
    ("已", "以"),
    ("向", "像"),
    ("分", "份"),
    ("份", "分"),
    ("后", "候"),
    ("候", "后"),
    ("进", "近"),
    ("近", "进"),
    # Second batch: still only genuinely high-frequency human confusions
    # (classic 错别字 pairs: 另令/刻克/题提/意义/明名/坐座/倍备/查察/长常/
    # 度渡/复覆/联连/园圆/功工/决绝/坚艰/各个/带戴/记纪/身生/净静/厉历/
    # 型形/采彩/暑署/观关/话画/废费/啦拉/清青/议意).
    ("另", "令"),
    ("刻", "克"),
    ("题", "提"),
    ("意", "义"),
    ("议", "意"),
    ("明", "名"),
    ("坐", "座"),
    ("倍", "备"),
    ("查", "察"),
    ("长", "常"),
    ("度", "渡"),
    ("复", "覆"),
    ("联", "连"),
    ("园", "圆"),
    ("功", "工"),
    ("决", "绝"),
    ("坚", "艰"),
    ("各", "个"),
    ("带", "戴"),
    ("记", "纪"),
    ("身", "生"),
    ("净", "静"),
    ("厉", "历"),
    ("型", "形"),
    ("采", "彩"),
    ("暑", "署"),
    ("观", "关"),
    ("话", "画"),
    ("废", "费"),
    ("啦", "拉"),
    ("清", "青"),
)

# char -> list of partners (bidirectional lookup)
_PARTNERS: dict[str, tuple[str, ...]] = {}
for _left, _right in _HOMOPHONE_PAIRS:
    _PARTNERS.setdefault(_left, ())
    if _right not in _PARTNERS[_left]:
        _PARTNERS[_left] = (*_PARTNERS[_left], _right)
    _PARTNERS.setdefault(_right, ())
    if _left not in _PARTNERS[_right]:
        _PARTNERS[_right] = (*_PARTNERS[_right], _left)


def typo_candidates(text: str) -> list[int]:
    """Positions in ``text`` whose character has a near-sound partner."""
    return [index for index, char in enumerate(text) if char in _PARTNERS]


def maybe_typo(text: str, *, error_rate: float = 0.03, seed: str = "") -> str:
    """Return ``text`` with at most one same/near-sound character swapped.

    Deterministic for the same (text, error_rate, seed). Never rewrites when
    the roll fails, the text is too short, or no eligible character exists.
    """
    if not isinstance(text, str) or not text:
        return text
    if not 0.0 < error_rate <= 1.0:
        return text
    if len(text) < _MIN_LENGTH:
        return text
    rng = random.Random(seed)
    if rng.random() >= error_rate:
        return text
    positions = typo_candidates(text)
    if not positions:
        return text
    index = positions[rng.randrange(len(positions))]
    partners = _PARTNERS[text[index]]
    replacement = partners[rng.randrange(len(partners))]
    return text[:index] + replacement + text[index + 1 :]


def should_typo(mood: str) -> bool:
    """Only high-arousal positive moods carry a believable slip."""
    return mood in TYPO_MOODS


def apply_typo(
    text: str, *, mood: str, enabled: bool, error_rate: float = 0.03, seed: str = ""
) -> str:
    """Dialogue-facing gate: config flag + mood + the pure roll in one call."""
    if not enabled or not isinstance(text, str) or not should_typo(mood or ""):
        return text
    return maybe_typo(text, error_rate=error_rate, seed=seed)
